# Switch Auto-Translator (CNX / Atmosphère)

Ferramenta desktop em Python para extração, tradução automatizada via Google Gemini API e empacotamento de patches **LayeredFS** (`atmosphere/contents/<TITLE_ID>/romfs`) para jogos de Nintendo Switch baseados na engine **CRI Middleware + Idea Factory / Otomate**.

## Recursos Técnicos

- **Desempacotamento e Reempacotamento CRI CPK (`cpk_handler.py`):** Leitura de tabelas `@UTF`, descompressão nativa **CRILAYLA** e reconstrução de arquivos `.CPK` mantendo alinhamento de setores.
- **Parser Binário STCM2L (`stcm2l_handler.py`):** Extração precisa de strings inline em scripts `.DAT` da Otomate, filtrando opcodes internos/variáveis da VM e recalculando automaticamente tamanhos de instrução, `word_count`, ponteiros relativos/absolutos, seção `EXPORT_DATA` e rodapé.
- **Injeção de Glifos Bitmap 4bpp (`font_handler.py`):** Engenharia reversa do formato proprietário `UF` (`.ffu`), compondo caracteres acentuados do Português (`á, à, â, ã, é, ê, í, ó, ô, õ, ú, ü, ç, º, ª`) a partir dos diacríticos nativos da fonte e atualizando as tabelas de busca Unicode e offsets de pixels.
- **Pipeline Resiliente com LLM (`translator.py`):** Descoberta dinâmica de modelos disponíveis na chave, rotação automática em caso de `429 RESOURCE_EXHAUSTED`, deduplicação de falas/nomes repetidos, validação de integridade de tags (`#Color`, `#n`) e checkpoint incremental em JSON.
- **Integração DBI MTP (`mtp_bridge.py` & `main.py`):** Interface gráfica em `CustomTkinter` com detecção automática do Title ID via USB (DBI MTP Responder).

## Instalação e Uso

O projeto utiliza o gerenciador de pacotes [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/SEU_USUARIO/switch-auto-translator.git
cd switch-auto-translator
uv sync
uv run main.py