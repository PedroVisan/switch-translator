import json
import os
import re
import time
from google import genai
from google.genai import types

SYSTEM_PROMPT = (
    "Você é um localizador profissional de visual novels de Nintendo Switch para Português do Brasil (PT-BR). "
    "Traduza os valores do objeto JSON recebido mantendo EXATAMENTE as mesmas chaves JSON. "
    "Preserve intactas todas as tags técnicas como #Color[0], #Color[3], #n, \\n, {variaveis}, [colchetes] e símbolos. "
    "Nunca traduza nomes próprios de personagens (ex: Hoshino, Ichika, Yanagi, Okazaki, Enomoto, Sasazuka, Shiraishi, Mochida, Saeki, Minegishi). "
    "Mantenha coerência de gênero, registre diálogos emocionais e termos de investigação de forma natural em PT-BR."
)

FREE_TIER_TEXT_MODELS = [
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
    "gemma-4-31b-it",
    "gemma-4-26b-a4b-it",
]

EXCLUDED_TOKENS = (
    "embedding",
    "imagen",
    "image",
    "veo",
    "tts",
    "audio",
    "live",
    "transcribe",
    "decision",
    "robotics",
    "2.5",
    "2.0",
    "1.5",
)

TAG_CHECK_RE = re.compile(r"(#Color\[\d+\]|#n|\\n|\{[^}]+\}|\[[^\]]+\])")


def _is_safe_translation(orig: str, translated: str) -> bool:
    if not isinstance(translated, str) or not translated.strip():
        return False
    if TAG_CHECK_RE.findall(orig) != TAG_CHECK_RE.findall(translated):
        return False
    if len(orig) <= 25 and len(translated) > 65:
        return False
    return True


def _discover_available_models(client: genai.Client) -> list[str]:
    found = set()
    try:
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            methods = getattr(m, "supported_generation_methods", None) or []
            if ("gemini" in name or "gemma" in name) and (
                not methods or "generateContent" in methods
            ):
                if not any(x in name for x in EXCLUDED_TOKENS):
                    found.add(name)
    except Exception as e:
        err = str(e)
        if "API_KEY_INVALID" in err or "API key expired" in err or "400" in err:
            raise RuntimeError(
                "A Gemini API Key foi rejeitada pelo Google. "
                "Gere uma nova chave em uma conta @gmail.com pessoal no Google AI Studio."
            )
        return list(FREE_TIER_TEXT_MODELS)

    ordered = [m for m in FREE_TIER_TEXT_MODELS if m in found]
    for m in sorted(found):
        if m not in ordered:
            ordered.append(m)
    return ordered or list(FREE_TIER_TEXT_MODELS)


def _parse_json_response(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end != -1 and end > start:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned)


def _call_model(client: genai.Client, model_name: str, prompt_json: str) -> dict:
    if model_name.startswith("gemma-"):
        full_prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"Retorne APENAS o objeto JSON traduzido, sem markdown ou texto extra:\n{prompt_json}"
        )
        resp = client.models.generate_content(
            model=model_name,
            contents=full_prompt,
            config=types.GenerateContentConfig(temperature=0.2),
        )
    else:
        resp = client.models.generate_content(
            model=model_name,
            contents=prompt_json,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
    return _parse_json_response(resp.text or "{}")


def translate_with_checkpoint(
    all_strings: dict[str, str],
    api_key: str,
    translated_out_path: str,
    batch_size: int = 120,
    log_fn=print,
    progress_fn=None,
) -> dict[str, str]:
    work_dir = os.path.dirname(translated_out_path)
    os.makedirs(work_dir, exist_ok=True)
    cache_path = os.path.join(work_dir, "translation_cache.json")

    cache: dict[str, str] = {}
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            cache = json.load(f)

    unique_texts = list(dict.fromkeys(all_strings.values()))
    pending_texts = [t for t in unique_texts if t not in cache]

    log_fn(
        f"[Cache] Total: {len(all_strings)} | Únicas: {len(unique_texts)} | "
        f"Já no cache: {len(unique_texts) - len(pending_texts)} | Faltam: {len(pending_texts)}"
    )

    api_keys = [k.strip() for k in api_key.split(",") if k.strip()]
    clients = [genai.Client(api_key=k) for k in api_keys]
    models = _discover_available_models(clients[0])
    log_fn(
        f"[Gemini] {len(clients)} chave(s) ativa(s) | "
        f"{len(models)} modelos na fila: {', '.join(models)}"
    )

    if pending_texts:
        total_batches = (len(pending_texts) + batch_size - 1) // batch_size
        client_idx = 0
        model_idx = 0

        for b_idx in range(total_batches):
            chunk = pending_texts[b_idx * batch_size : (b_idx + 1) * batch_size]
            payload = {f"u_{i:04d}": txt for i, txt in enumerate(chunk)}
            prompt_json = json.dumps(payload, ensure_ascii=False)

            translated_batch = None
            attempts = 0
            max_attempts = len(clients) * len(models) * 2

            while translated_batch is None and attempts < max_attempts:
                attempts += 1
                curr_client = clients[client_idx % len(clients)]
                curr_model = models[model_idx % len(models)]

                try:
                    parsed = _call_model(curr_client, curr_model, prompt_json)
                    if isinstance(parsed, dict):
                        translated_batch = parsed
                except Exception as err:
                    err_str = str(err)
                    if "API_KEY_INVALID" in err_str or "API key expired" in err_str:
                        raise RuntimeError(
                            "Chave de API inválida ou expirada pelo Google. "
                            "Crie uma chave nova em uma conta @gmail.com pessoal."
                        )
                    if "404" in err_str or "NOT_FOUND" in err_str:
                        log_fn(f"[Indisponível 404] Pulando {curr_model}...")
                        model_idx += 1
                    elif "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        log_fn(
                            f"[Limite 429] Aguardando 15s e alternando modelo/chave ({curr_model})..."
                        )
                        client_idx += 1
                        if client_idx % len(clients) == 0:
                            model_idx += 1
                        time.sleep(15.0)
                    elif "503" in err_str or "UNAVAILABLE" in err_str:
                        log_fn(f"[Servidor 503] Tentando novamente em 5s ({curr_model})...")
                        time.sleep(5.0)
                    else:
                        log_fn(f"[Aviso API] {curr_model}: {err_str[:120]}")
                        model_idx += 1
                        time.sleep(2.0)

            if translated_batch is None:
                raise RuntimeError(
                    "Cota diária esgotada em todas as chaves/modelos disponíveis. "
                    "O progresso foi salvo no translation_cache.json; retome mais tarde."
                )

            accepted = 0
            for i, orig_v in enumerate(chunk):
                key = f"u_{i:04d}"
                val = translated_batch.get(key)
                if _is_safe_translation(orig_v, val):
                    cache[orig_v] = val
                    accepted += 1

            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)

            frac = 0.05 + 0.87 * ((b_idx + 1) / total_batches)
            if progress_fn:
                progress_fn(
                    frac,
                    f"Traduzindo lote {b_idx + 1}/{total_batches} ({int(frac * 100)}%)",
                )
            log_fn(
                f"[Lote {b_idx + 1}/{total_batches}] {accepted}/{len(chunk)} falas salvas no cache."
            )
            time.sleep(4.5)

    final_map = {sid: cache.get(orig, orig) for sid, orig in all_strings.items()}
    with open(translated_out_path, "w", encoding="utf-8") as f:
        json.dump(final_map, f, ensure_ascii=False, indent=2)

    return final_map