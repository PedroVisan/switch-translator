import json
import os
import re
import time
from google import genai
from google.genai import types

SYSTEM_PROMPT = (
    "Você é um localizador profissional de jogos de Nintendo Switch para Português do Brasil (PT-BR). "
    "Traduza os valores do objeto JSON recebido mantendo EXATAMENTE as mesmas chaves JSON. "
    "Preserve intactas todas as tags técnicas como #Color[0], #Color[3], #n, \\n, {variaveis}, [colchetes] e símbolos. "
    "Nunca traduza nomes próprios de personagens (ex: Hoshino, Yanagi, Okazaki, Enomoto, Sasazuka, Shiraishi, Mochida, Saeki). "
    "Adapte o tom dos diálogos de forma natural ao português brasileiro."
)

TAG_CHECK_RE = re.compile(r"(#Color\[\d+\]|#n|\\n|\{[^}]+\}|\[[^\]]+\])")


def _is_safe_translation(orig: str, translated: str) -> bool:
    """Garante que a IA não deslocou chaves nem corrompeu tags técnicas."""
    if not isinstance(translated, str) or not translated.strip():
        return False
    if TAG_CHECK_RE.findall(orig) != TAG_CHECK_RE.findall(translated):
        return False
    if len(orig) <= 25 and len(translated) > 65:
        return False
    return True


def _discover_available_models(client: genai.Client) -> list[str]:
    """Consulta a API e ordena os modelos Gemini disponíveis do mais rápido para o mais capaz."""
    preferred_order = [
        "gemini-2.5-flash",
        "gemini-2.5-flash-lite",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
        "gemini-1.5-flash",
        "gemini-2.5-pro",
    ]
    found = []
    try:
        for m in client.models.list():
            name = (m.name or "").replace("models/", "")
            methods = getattr(m, "supported_generation_methods", None) or []
            if "gemini" in name and "generateContent" in methods:
                if not any(x in name for x in ("embedding", "imagen", "veo", "tts", "audio")):
                    found.append(name)
    except Exception:
        return preferred_order

    ordered = [m for m in preferred_order if m in found]
    for m in found:
        if m not in ordered:
            ordered.append(m)
    return ordered or preferred_order


def _parse_json_response(raw_text: str) -> dict:
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def translate_with_checkpoint(
    all_strings: dict[str, str],
    api_key: str,
    translated_out_path: str,
    batch_size: int = 250,
    log_fn=print,
    progress_fn=None,
) -> dict[str, str]:
    """
    Deduplica textos idênticos, consulta o cache incremental (translation_cache.json),
    traduz apenas o inédito com rotação automática de chaves/modelos e salva o JSON final.
    """
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
    log_fn(f"[Gemini] {len(clients)} chave(s) ativa(s) | Fila de modelos: {', '.join(models[:4])}")

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
                    resp = curr_client.models.generate_content(
                        model=curr_model,
                        contents=prompt_json,
                        config=types.GenerateContentConfig(
                            system_instruction=SYSTEM_PROMPT,
                            response_mime_type="application/json",
                            temperature=0.2,
                        ),
                    )
                    parsed = _parse_json_response(resp.text or "{}")
                    if isinstance(parsed, dict):
                        translated_batch = parsed
                except Exception as err:
                    err_str = str(err)
                    if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
                        log_fn(f"[Cota 429] Trocando modelo/chave ({curr_model})...")
                        client_idx += 1
                        if client_idx % len(clients) == 0:
                            model_idx += 1
                        time.sleep(2.0)
                    elif "503" in err_str or "UNAVAILABLE" in err_str:
                        log_fn(f"[Servidor 503] Tentando novamente em 3s ({curr_model})...")
                        time.sleep(3.0)
                    else:
                        log_fn(f"[Aviso API] {curr_model}: {err_str[:120]}")
                        model_idx += 1
                        time.sleep(1.5)

            if translated_batch is None:
                raise RuntimeError(
                    "Cota diária esgotada em todas as chaves/modelos. "
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
            log_fn(f"[Lote {b_idx + 1}/{total_batches}] {accepted}/{len(chunk)} falas salvas no cache.")
            time.sleep(1.2)

    final_map = {sid: cache.get(orig, orig) for sid, orig in all_strings.items()}
    with open(translated_out_path, "w", encoding="utf-8") as f:
        json.dump(final_map, f, ensure_ascii=False, indent=2)

    return final_map