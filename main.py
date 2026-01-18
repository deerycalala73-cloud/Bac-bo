import os
import asyncio
import json
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
import pytz

import aiohttp
from telegram import Bot
from telegram.error import TelegramError
from dotenv import load_dotenv

load_dotenv()

# Configurações
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "8308362105:AAELmmAUIcTgbJ3xozM1mhsLPk-8EqOSOgY")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "-1003278747270")

API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

ANGOLA_TZ = pytz.timezone('Africa/Luanda')

OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡",
    "🔵": "🔵",
    "🔴": "🔴",
    "🟡": "🟡",
}

PADROES = [
    {"id": 10, "sequencia": ["🔵", "🔴"], "sinal": "🔵"},
    # {"id": 11, "sequencia": ["🔴", "🔵", "🔴"], "sinal": "🔵"},  # exemplo - descomenta se quiser testar mais padrões
]

API_POLL_INTERVAL = 3
SIGNAL_CYCLE_INTERVAL = 5

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-5s | %(message)s'
)
logger = logging.getLogger("BacBoBot")

# Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Estado global
state: Dict[str, Any] = {
    "history": [],
    "last_round_id": None,
    "waiting_for_result": False,
    "last_signal_color": None,
    "martingale_count": 0,
    "entrada_message_id": None,
    "martingale_message_ids": [],
    "greens_seguidos": 0,
    "total_greens": 0,
    "total_empates": 0,
    "total_losses": 0,
    "last_signal_pattern_id": None,
    "last_signal_sequence": None,
    "last_signal_round_id": None,
    "signal_cooldown": False,
    "analise_message_id": None,
    "last_reset_date": None,
}


async def send_to_channel(text: str, parse_mode="HTML") -> Optional[int]:
    """Envia mensagem pro canal e retorna o message_id ou None em caso de erro"""
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        return msg.message_id
    except TelegramError as te:
        logger.error("Erro Telegram API: %s - %s", te.error_code, te.message)
        return None
    except Exception as e:
        logger.exception("Erro inesperado ao enviar mensagem pro canal")
        return None


async def send_error_to_channel(error_msg: str):
    """Envia erros importantes diretamente pro canal"""
    timestamp = datetime.now(ANGOLA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"⚠️ <b>ERRO DETECTADO</b> ⚠️\n"
        f"<code>{timestamp}</code>\n\n"
        f"{error_msg}\n\n"
        f"<i>O bot continua tentando funcionar...</i>"
    )
    await send_to_channel(text)


async def delete_messages(message_ids: List[int]):
    if not message_ids:
        return
    for mid in message_ids[:]:  # cópia pra evitar modificação durante iteração
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except Exception:
            pass


# ================== Funções principais ==================
async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict]:
    try:
        async with session.get(API_URL, headers=HEADERS, timeout=12) as resp:
            if resp.status != 200:
                await send_error_to_channel(
                    f"API retornou status <b>{resp.status}</b>\nURL: <code>{API_URL}</code>"
                )
                return None
            return await resp.json()
    except asyncio.TimeoutError:
        await send_error_to_channel("Timeout na consulta à API (12s)")
        return None
    except Exception as e:
        await send_error_to_channel(f"Erro grave na API:\n<code>{str(e)}</code>")
        return None


# ... (outras funções mantidas quase iguais, só com pequenos ajustes)

# Versão melhorada do update_history_from_api
async def update_history_from_api(session: aiohttp.ClientSession):
    reset_placar_if_needed()

    data = await fetch_api(session)
    if not data:
        return

    try:
        # Tentar diferentes estruturas possíveis da API
        if isinstance(data, dict):
            if "data" in data:
                data = data["data"]
            round_id = data.get("id")
            outcome_raw = (data.get("result") or {}).get("outcome")
        else:
            round_id = None
            outcome_raw = None

        if not round_id or not outcome_raw:
            logger.warning("Dados incompletos da API")
            return

        outcome_emoji = OUTCOME_MAP.get(outcome_raw)
        if not outcome_emoji:
            # Tentativa de normalização extra
            s = str(outcome_raw).lower()
            if any(x in s for x in ["player", "jogador"]):
                outcome_emoji = "🔵"
            elif any(x in s for x in ["banker", "banco"]):
                outcome_emoji = "🔴"
            elif any(x in s for x in ["tie", "empate", "draw"]):
                outcome_emoji = "🟡"

        if not outcome_emoji:
            await send_error_to_channel(f"Outcome não reconhecido: <code>{outcome_raw}</code>")
            return

        if state["last_round_id"] != round_id:
            state["last_round_id"] = round_id
            state["history"].append(outcome_emoji)
            if len(state["history"]) > 200:
                state["history"].pop(0)
            logger.info(f"Novo resultado → {outcome_emoji} | round: {round_id}")
            state["signal_cooldown"] = False

    except Exception as e:
        await send_error_to_channel(f"Erro ao processar dados da API:\n<code>{str(e)}</code>")


# Principal loop com try/except mais robusto
async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await update_history_from_api(session)
                await resolve_after_result()
            except Exception as e:
                logger.exception("Erro crítico no api_worker")
                await send_error_to_channel(
                    f"Erro grave no loop principal da API:\n<code>{str(e)}</code>"
                )
                await asyncio.sleep(8)  # espera um pouco mais em caso de erro grave
            await asyncio.sleep(API_POLL_INTERVAL)


async def scheduler_worker():
    await asyncio.sleep(3)  # pequeno delay inicial
    while True:
        try:
            await try_send_signal()
        except Exception as e:
            logger.exception("Erro no scheduler_worker")
            await send_error_to_channel(f"Erro no envio de sinais:\n<code>{str(e)}</code>")
            await asyncio.sleep(5)
        await asyncio.sleep(SIGNAL_CYCLE_INTERVAL)


# ===================== INÍCIO =====================
async def main():
    logger.info("🚀 Bot iniciado...")
    await send_to_channel("🤖 <b>Bot iniciado</b> — procurando sinais...")

    # Inicia as duas tarefas principais
    await asyncio.gather(
        api_worker(),
        scheduler_worker(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário (Ctrl+C)")
    except Exception as e:
        logger.critical("Erro fatal ao iniciar o bot", exc_info=True)
        # Tenta enviar até o último suspiro...
        asyncio.run(send_error_to_channel(
            f"<b>ERRO FATAL</b> — O bot caiu!\n\n<code>{str(e)}</code>"
        ))
