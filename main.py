import os
import asyncio
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
    # Adicione mais padrões conforme necessário
    # {"id": 11, "sequencia": ["🔴", "🔵", "🔴"], "sinal": "🔵"},
]

API_POLL_INTERVAL = 3      # segundos
SIGNAL_CYCLE_INTERVAL = 5  # segundos

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


# ================== Funções de envio ==================
async def send_to_channel(text: str, parse_mode="HTML") -> Optional[int]:
    try:
        msg = await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=parse_mode,
            disable_web_page_preview=True
        )
        return msg.message_id
    except TelegramError as te:
        logger.error("Erro Telegram: %s - %s", te.error_code, te.message)
        return None
    except Exception as e:
        logger.exception("Erro ao enviar mensagem")
        return None


async def send_error_to_channel(error_msg: str):
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
    for mid in message_ids[:]:
        try:
            await bot.delete_message(TELEGRAM_CHANNEL_ID, mid)
        except Exception:
            pass


# ================== Funções de controle do placar ==================
def should_reset_placar() -> bool:
    """Verifica se deve resetar o placar (novo dia em Angola)"""
    now_angola = datetime.now(ANGOLA_TZ)
    current_date = now_angola.date()
    
    if state["last_reset_date"] is None or state["last_reset_date"] != current_date:
        state["last_reset_date"] = current_date
        return True
    return False


def reset_placar_if_needed():
    """Reseta contadores do placar se necessário"""
    if should_reset_placar():
        state["total_greens"] = 0
        state["total_empates"] = 0
        state["total_losses"] = 0
        state["greens_seguidos"] = 0
        logger.info("🔄 Placar resetado - novo dia em Angola")


# ================== API e processamento ==================
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


async def update_history_from_api(session: aiohttp.ClientSession):
    reset_placar_if_needed()

    data = await fetch_api(session)
    if not data:
        return

    try:
        # Tentar diferentes estruturas possíveis da resposta
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


# ================== Lógica de sinais ==================
def history_ends_with(history: List[str], seq: List[str]) -> bool:
    n = len(seq)
    if n == 0 or len(history) < n:
        return False
    return history[-n:] == seq


def find_matching_pattern(history: List[str]) -> Optional[Dict]:
    for pat in PADROES:
        if history_ends_with(history, pat["sequencia"]):
            return pat
    return None


def main_entry_text(color_emoji: str) -> str:
    if color_emoji == "🔵":
        return (
            "𝗔𝗭𝗨𝗟 🔵\n"
            "𝗖𝗢𝗕𝗥𝗘 𝗘𝗠𝗣𝗔𝗧𝗘 🟡\n\n"
            "𝗦𝗢𝗠𝗘𝗡𝗧𝗘 𝗚𝗔𝗟𝗘 1\n\n"
            "𝗝𝗢𝗚𝗨𝗘 𝗖𝗢𝗠 𝗥𝗘𝗦𝗣𝗢𝗡𝗦𝗔𝗕𝗜𝗟𝗜𝗗𝗔𝗗𝗘"
        )
    else:
        return (
            "𝗩𝗘𝗥𝗠𝗘𝗟𝗛𝗢 🔴\n"
            "𝗖𝗢𝗕𝗥𝗘 𝗘𝗠𝗣𝗔𝗧𝗘 🟡\n\n"
            "𝗦𝗢𝗠𝗘𝗡𝗧𝗘 𝗚𝗔𝗟𝗘 1\n\n"
            "𝗝𝗢𝗚𝗨𝗘 𝗖𝗢𝗠 𝗥𝗘𝗦𝗣𝗢𝗡𝗦𝗔𝗕𝗜𝗟𝗜𝗗𝗔𝗗𝗘"
        )


def martingale_text(color_emoji: str) -> str:
    return "➡️ Vamos para o 1ª gale"


def green_text(greens_seguidos: int) -> str:
    return f"🔥 Estamos a {greens_seguidos} vitória(s) seguida(s)!\nPAGA BLACK G1"


def analise_text() -> str:
    return "🔍 <b>ANALISANDO...</b> 🔍"


async def resolve_after_result():
    if not state["waiting_for_result"] or not state["last_signal_color"]:
        return

    if not state["history"]:
        return

    last_outcome = state["history"][-1]

    if state["last_result_round_id"] == state["last_round_id"]:
        return

    if state["last_signal_round_id"] == state["last_round_id"]:
        logger.info("⏳ Aguardando próximo round para verificar resultado...")
        return

    state["last_result_round_id"] = state["last_round_id"]
    target = state["last_signal_color"]

    # Empate
    if last_outcome == "🟡":
        state["greens_seguidos"] += 1
        state["total_empates"] += 1
        await send_to_channel(green_text(state["greens_seguidos"]))
        await send_to_channel(format_placar())
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True
        return

    # Green
    if last_outcome == target:
        state["greens_seguidos"] += 1
        state["total_greens"] += 1
        await send_to_channel(green_text(state["greens_seguidos"]))
        await send_to_channel(format_placar())
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True
        return

    # Martingale ou Loss
    if state["martingale_count"] == 0:
        state["martingale_count"] += 1
        msg_id = await send_to_channel(martingale_text(target))
        if msg_id:
            state["martingale_message_ids"].append(msg_id)
        return
    else:
        state["greens_seguidos"] = 0
        state["total_losses"] += 1
        await send_to_channel("🟥 <b>LOSS 🟥</b>")
        await send_to_channel(format_placar())
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True
        return


async def send_analise_message():
    if state["analise_message_id"] is None and not state["waiting_for_result"]:
        msg_id = await send_to_channel(analise_text())
        if msg_id:
            state["analise_message_id"] = msg_id
            logger.info("📤 Mensagem de análise enviada")


async def delete_analise_message():
    if state["analise_message_id"] is not None:
        await delete_messages([state["analise_message_id"]])
        state["analise_message_id"] = None
        logger.info("🗑️ Mensagem de análise apagada")


async def try_send_signal():
    if state["waiting_for_result"]:
        await send_analise_message()
        return

    if state["signal_cooldown"]:
        await send_analise_message()
        return

    if len(state["history"]) < 5:
        logger.info(f"Histórico curto ({len(state['history'])}), aguardando...")
        await send_analise_message()
        return

    pat = find_matching_pattern(state["history"])
    if not pat:
        await send_analise_message()
        return

    sinal = pat["sinal"]
    color = sinal if sinal in ("🔵", "🔴") else "🔵"  # fallback

    current_sequence = state["history"][-len(pat["sequencia"]):]
    if (state["last_signal_pattern_id"] == pat["id"] and 
        state["last_signal_sequence"] == current_sequence):
        await send_analise_message()
        return

    await delete_analise_message()
    await delete_messages(state["martingale_message_ids"])
    state["martingale_message_ids"] = []

    msg_id = await send_to_channel(main_entry_text(color))
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = color
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = pat["id"]
        state["last_signal_sequence"] = current_sequence
        state["last_signal_round_id"] = state["last_round_id"]
        logger.info(f"Sinal enviado: {color} (padrão {pat['id']})")


def format_placar() -> str:
    return (
        "🏆 PLACAR DO DIA 🏆\n"
        f"✅ GREENS: {state['total_greens']}\n"
        f"🤝 EMPATES: {state['total_empates']}\n"
        f"⛔ LOSS: {state['total_losses']}"
    )


# ================== Workers ==================
async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await update_history_from_api(session)
                await resolve_after_result()
            except Exception as e:
                logger.exception("Erro no api_worker")
                await send_error_to_channel(f"Erro grave no loop da API:\n<code>{str(e)}</code>")
                await asyncio.sleep(8)
            await asyncio.sleep(API_POLL_INTERVAL)


async def scheduler_worker():
    await asyncio.sleep(3)
    while True:
        try:
            await try_send_signal()
        except Exception as e:
            logger.exception("Erro no scheduler_worker")
            await send_error_to_channel(f"Erro no envio de sinais:\n<code>{str(e)}</code>")
            await asyncio.sleep(5)
        await asyncio.sleep(SIGNAL_CYCLE_INTERVAL)


# ================== Inicialização ==================
async def main():
    logger.info("🚀 Bot iniciado...")
    await send_to_channel("🤖 <b>Bot iniciado</b> — procurando sinais...")

    await asyncio.gather(
        api_worker(),
        scheduler_worker(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot parado pelo usuário")
    except Exception as e:
        logger.critical("Erro fatal ao iniciar o bot", exc_info=True)
        asyncio.run(send_error_to_channel(
            f"<b>ERRO FATAL</b> — O bot caiu!\n\n<code>{str(e)}</code>"
        ))
