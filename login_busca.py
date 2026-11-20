"""Liga a busca global de canais do bot. Rode uma vez só:  python login_busca.py

Precisa de TG_API_ID e TG_API_HASH no .env (pegue em https://my.telegram.org > API development tools).
Ele pede o seu telefone e o código que o Telegram manda, e cria o arquivo busca.session.
Esse arquivo dá acesso à conta: não compartilhe. Use de preferência um número secundário.
"""
import asyncio
import os
from pathlib import Path

from telethon import TelegramClient

PASTA = Path(__file__).parent
for linha in (PASTA / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in linha and not linha.strip().startswith("#"):
        k, v = linha.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())

api_id = int(os.environ.get("TG_API_ID", "0") or 0)
api_hash = os.environ.get("TG_API_HASH", "")
if not (api_id and api_hash):
    raise SystemExit("Preencha TG_API_ID e TG_API_HASH no .env primeiro (https://my.telegram.org).")


async def main() -> None:
    async with TelegramClient(str(PASTA / "busca"), api_id, api_hash) as c:
        eu = await c.get_me()
        print(f"\nPronto! Busca ligada com a conta {eu.first_name}. Reinicie o bot.")


asyncio.run(main())
