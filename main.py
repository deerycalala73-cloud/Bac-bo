import os
import asyncio
import json
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
import pytz

import aiohttp
from telegram import Bot
from dotenv import load_dotenv

load_dotenv()

# Configurações (substitui ou usa .env)
TELEGRAM_BOT_TOKEN = "8308362105:AAELmmAUIcTgbJ3xozM1mhsLPk-8EqOSOgY"
TELEGRAM_CHANNEL_ID = "-1003278747270"

# URL da API REAL que funciona
API_URL = "https://api-cs.casino.org/svc-evolution-game-events/api/bacbo/latest"

# Headers para a API
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'application/json',
    'Accept-Language': 'en-US,en;q=0.9',
}

# Fuso horário de Angola (WAT - West Africa Time)
ANGOLA_TZ = pytz.timezone('Africa/Luanda')

# Mapeamento de resultados
OUTCOME_MAP = {
    "PlayerWon": "🔵",
    "BankerWon": "🔴",
    "Tie": "🟡",
    # Caso a API já retorne emojis:
    "🔵": "🔵",
    "🔴": "🔴",
    "🟡": "🟡",
}

# Padrões (usa os que sabes, crlh)
PADROES = [
    {"id": 10, "sequencia": ["🔵", "🔴"], "sinal": "🔵"},
]

# Temporizadores
API_POLL_INTERVAL = 3         # segundos entre polls
SIGNAL_CYCLE_INTERVAL = 5     # segundos entre tentativas de enviar sinal

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
log = logging.getLogger("bacbo_fixed")

# Bot
bot = Bot(token=TELEGRAM_BOT_TOKEN)

# Estado
state: Dict[str, Any] = {
    "history": [],                       # lista de emojis (mais recente no fim)
    "last_round_id": None,
    "waiting_for_result": False,
    "last_signal_color": None,           # "🔵" ou "🔴"
    "martingale_count": 0,
    "entrada_message_id": None,
    "martingale_message_ids": [],
    "greens_seguidos": 0,
    "total_greens": 0,                   # Total de greens (acertos da cor)
    "total_empates": 0,                  # Total de empates
    "total_losses": 0,                   # Total de losses
    "last_signal_pattern_id": None,      # ID do último padrão que gerou sinal
    "last_signal_sequence": None,        # Sequência que gerou o último sinal
    "last_empate_round_id": None,        # Último round que foi empate
    "last_result_round_id": None,        # Último round que foi processado para resultado
    "signal_cooldown": False,            # Evita múltiplos sinais consecutivos
    "analise_message_id": None,          # ID da mensagem de análise
    "last_signal_round_id": None,        # Round ID quando o sinal foi enviado
    "last_reset_date": None,             # Data do último reset do placar
}


# ---------- Funções utilitárias ----------
def history_ends_with(history: List[str], seq: List[str]) -> bool:
    n = len(seq)
    if n == 0 or len(history) < n:
        return False
    return history[-n:] == seq


def find_matching_pattern(history: List[str]) -> Optional[Dict[str, Any]]:
    for pat in PADROES:
        if history_ends_with(history, pat["sequencia"]):
            return pat
    return None


def should_reset_placar():
    """Verifica se deve resetar o placar (meia-noite em Angola)"""
    now_angola = datetime.now(ANGOLA_TZ)
    current_date = now_angola.date()
    
    # Se é a primeira vez ou se a data mudou
    if state["last_reset_date"] is None or state["last_reset_date"] != current_date:
        state["last_reset_date"] = current_date
        return True
    return False


def reset_placar_if_needed():
    """Reseta o placar se for meia-noite em Angola"""
    if should_reset_placar():
        state["total_greens"] = 0
        state["total_empates"] = 0
        state["total_losses"] = 0
        state["greens_seguidos"] = 0
        log.info("🔄 Placar resetado - novo dia em Angola")


def format_placar() -> str:
    return (
        "🏆 PLACAR DO DIA 🏆\n"
        f"✅ GREENS: {state['total_greens']}\n"
        f"🤝 EMPATES: {state['total_empates']}\n"
        f"⛔ LOSS: {state['total_losses']}"
    )


# ---------- API ----------
async def fetch_api(session: aiohttp.ClientSession) -> Optional[Dict[str, Any]]:
    try:
        log.info(f"📡 Consultando API: {API_URL}")
        async with session.get(API_URL, headers=HEADERS, timeout=10) as resp:
            if resp.status == 200:
                data = await resp.json()
                log.info(f"✅ API retornou status 200")
                return data
            else:
                log.warning(f"⚠️ API retornou status {resp.status}")
                return None
    except asyncio.TimeoutError:
        log.warning("⏱️ Timeout na consulta à API")
        return None
    except Exception as e:
        log.error(f"❌ Erro na API: {e}")
        return None


async def update_history_from_api(session: aiohttp.ClientSession):
    """
    Puxa o último round da API e atualiza history/round id.
    Espera por mudanças de round id para adicionar novo outcome no histórico.
    """
    # Verificar se precisa resetar o placar
    reset_placar_if_needed()
    
    data = await fetch_api(session)
    if not data:
        log.warning("📭 Nenhum dado retornado da API")
        return

    # Estrutura da API: {"id":"...","data":{"id":"...","result":{"outcome":"..."}}}
    if not isinstance(data, dict):
        log.warning("⚠️ Dados da API não são um dicionário")
        return

    # Extrair dados da estrutura
    if "data" in data and isinstance(data["data"], dict):
        data = data["data"]
    
    if not isinstance(data, dict):
        log.warning("⚠️ Dados 'data' não são um dicionário")
        return

    # Extrair informações do round
    round_id = data.get("id")
    result = data.get("result") or {}
    outcome_raw = result.get("outcome")

    if not round_id or not outcome_raw:
        log.warning(f"📋 Dados incompletos: round_id={round_id}, outcome={outcome_raw}")
        return

    # Normalizar outcome para emoji
    outcome_emoji = None
    if outcome_raw in OUTCOME_MAP:
        outcome_emoji = OUTCOME_MAP[outcome_raw]
    else:
        if isinstance(outcome_raw, str):
            s = outcome_raw.lower()
            if "player" in s:
                outcome_emoji = OUTCOME_MAP["PlayerWon"]
            elif "banker" in s:
                outcome_emoji = OUTCOME_MAP["BankerWon"]
            elif "tie" in s or "empate" in s or "draw" in s:
                outcome_emoji = OUTCOME_MAP["Tie"]
            elif outcome_raw in ("🔵", "🔴", "🟡"):
                outcome_emoji = outcome_raw

    if not outcome_emoji:
        log.warning(f"❓ Outcome não reconhecido: {outcome_raw}")
        return

    # Se round mudou, adiciona ao histórico
    if state["last_round_id"] != round_id:
        state["last_round_id"] = round_id
        state["history"].append(outcome_emoji)
        # limitar histórico
        if len(state["history"]) > 200:
            state["history"].pop(0)
        log.info(f"📊 Novo round {round_id} -> {outcome_emoji}. Histórico: {len(state['history'])}")
        
        # Reset cooldown quando temos um novo round
        state["signal_cooldown"] = False
    else:
        log.debug(f"⏭️ Round {round_id} ainda não mudou")


# ---------- Mensagens ----------
async def send_message(text: str) -> Optional[int]:
    try:
        msg = await bot.send_message(chat_id=TELEGRAM_CHANNEL_ID, text=text, parse_mode="HTML")
        return msg.message_id
    except Exception as e:
        log.exception("❌ Erro ao enviar mensagem: %s", e)
        return None


async def delete_messages(ids: List[int]):
    for m in ids:
        try:
            await bot.delete_message(chat_id=TELEGRAM_CHANNEL_ID, message_id=m)
        except Exception:
            pass


def main_entry_text(color_emoji: str) -> str:
    if color_emoji == "🔵":
        return (
            "𝗔𝗭𝗨𝗟 🔵\n"
            "𝗖𝗢𝗕𝗥𝗘 𝗘𝗠𝗣𝗔𝗧𝗘 🟡\n\n"
            "𝗦𝗢𝗠𝗘𝗡𝗧𝗘 𝗚𝗔𝗟𝗘 1\n\n"
            "𝗝𝗢𝗚𝗨𝗘 𝗖𝗢𝗠 𝗥𝗘𝗦𝗣𝗢𝗡𝗦𝗔𝗕𝗟𝗜𝗗𝗔𝗗𝗘"
        )
    else:
        return (
            "𝗩𝗘𝗥𝗠𝗘𝗟𝗛𝗢 🔴\n"
            "𝗖𝗢𝗕𝗥𝗘 𝗘𝗠𝗣𝗔𝗧𝗘 🟡\n\n"
            "𝗦𝗢𝗠𝗘𝗡𝗧𝗘 𝗚𝗔𝗟𝗘 1\n\n"
            "𝗝𝗢𝗚𝗨𝗘 𝗖𝗢𝗠 𝗥𝗘𝗦𝗣𝗢𝗡𝗦𝗔𝗕𝗟𝗜𝗗𝗔𝗗𝗘"
        )


def martingale_text(color_emoji: str) -> str:
    return "➡️ Vamos para o 1ª gale"


def green_text(greens_seguidos: int) -> str:
    return f"🔥 Estamos a {greens_seguidos} vitória(s) seguida(s)!\nPAGA BLACK G1"


def analise_text() -> str:
    return "🔍 <b>ANALISANDO...</b> 🔍"


# ---------- Lógica de decisão ----------
async def resolve_after_result():
    """
    Se existe um sinal pendente, resolve com o último resultado do history.
    Aplica 1 gale no máximo.
    EMPATE agora é considerado como GREEN.
    """
    if not state["waiting_for_result"] or not state["last_signal_color"]:
        return

    # pega ultimo resultado (mais recente)
    if not state["history"]:
        return
    last_outcome = state["history"][-1]

    # Verificar se já processamos este round para evitar duplicação
    if state["last_result_round_id"] == state["last_round_id"]:
        return

    # CRÍTICO: Só processar resultado se o round for DIFERENTE do round em que o sinal foi enviado
    if state["last_signal_round_id"] == state["last_round_id"]:
        log.info("⏳ Aguardando próximo round para verificar resultado...")
        return
        
    state["last_result_round_id"] = state["last_round_id"]

    target = state["last_signal_color"]

    # Verificar se é EMPATE
    if last_outcome == "🟡":
        # EMPATE - contar como empate separado
        state["greens_seguidos"] += 1
        state["total_empates"] += 1
        
        # Enviar mensagem de green com contagem de vitórias seguidas
        await send_message(green_text(state["greens_seguidos"]))
        # Enviar placar atualizado
        await send_message(format_placar())
            
        # limpar martingale messages e resetar estado
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True  # Ativar cooldown após green
        return

    # Verificar se é GREEN (acerto da cor)
    if last_outcome == target:
        # GREEN (acerto da cor)
        state["greens_seguidos"] += 1
        state["total_greens"] += 1
        
        # Enviar mensagem de green com contagem de vitórias seguidas
        await send_message(green_text(state["greens_seguidos"]))
        # Enviar placar atualizado
        await send_message(format_placar())
            
        # limpar martingale messages e resetar estado
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True  # Ativar cooldown após green
        return

    # Primeira entrada não deu Green - verificar se é a primeira tentativa
    if state["martingale_count"] == 0:
        # Primeira entrada não deu Green - enviar MARTINGALE 1
        state["martingale_count"] += 1
        msg_id = await send_message(martingale_text(target))
        if msg_id:
            state["martingale_message_ids"].append(msg_id)
        # aguardamos próximo round para verificar martingale
        return
    else:
        # já usou martingale -> LOSS
        state["greens_seguidos"] = 0
        state["total_losses"] += 1
        await send_message("🟥 <b>LOSS 🟥</b>")
        # Enviar placar atualizado após loss
        await send_message(format_placar())
        await delete_messages(state["martingale_message_ids"])
        state["martingale_message_ids"] = []
        state["waiting_for_result"] = False
        state["last_signal_color"] = None
        state["martingale_count"] = 0
        state["last_signal_pattern_id"] = None
        state["last_signal_sequence"] = None
        state["last_signal_round_id"] = None
        state["signal_cooldown"] = True  # Ativar cooldown após loss
        return


async def send_analise_message():
    """Envia mensagem de análise se não existe uma pendente"""
    if state["analise_message_id"] is None and not state["waiting_for_result"]:
        msg_id = await send_message(analise_text())
        if msg_id:
            state["analise_message_id"] = msg_id
            log.info("📤 Mensagem de análise enviada: %s", msg_id)


async def delete_analise_message():
    """Apaga mensagem de análise se existir"""
    if state["analise_message_id"] is not None:
        await delete_messages([state["analise_message_id"]])
        state["analise_message_id"] = None
        log.info("🗑️ Mensagem de análise apagada")


async def try_send_signal():
    """
    A cada ciclo de SIGNAL_CYCLE_INTERVAL, tenta detectar padrão e enviar sinal
    apenas se não há sinal pendente.
    """
    if state["waiting_for_result"]:
        log.info("⏸️ Há sinal pendente — não enviar novo")
        # Enviar mensagem de análise se não houver sinal pendente
        await send_analise_message()
        return
        
    if state["signal_cooldown"]:
        log.info("⏸️ Em cooldown — não enviar novo sinal")
        # Enviar mensagem de análise se não houver sinal pendente
        await send_analise_message()
        return

    # Se histórico muito curto, aguardar mais dados
    if len(state["history"]) < 5:
        log.info(f"📊 Histórico muito curto ({len(state['history'])}), aguardando mais dados...")
        # Enviar mensagem de análise se não houver sinal pendente
        await send_analise_message()
        return

    # tentar detectar padrão
    pat = find_matching_pattern(state["history"])
    if not pat:
        log.debug("🔍 Nenhum padrão detectado")
        # Enviar mensagem de análise se não encontrou padrão
        await send_analise_message()
        return

    sinal = pat["sinal"]
    color = sinal if sinal in ("🔵", "🔴") else ( "🔵" if sinal == "🔵" else sinal )

    # Verificar se é o mesmo padrão e sequência do último sinal enviado
    current_sequence = state["history"][-len(pat["sequencia"]):]
    if (state["last_signal_pattern_id"] == pat["id"] and 
        state["last_signal_sequence"] == current_sequence):
        log.info("⏭️ Padrão %s com mesma sequência já foi enviado — ignorando", pat["id"])
        # Enviar mensagem de análise se padrão já foi enviado
        await send_analise_message()
        return

    # Antes de enviar nova entrada, apagar mensagem de análise e martingale antigas
    await delete_analise_message()
    await delete_messages(state["martingale_message_ids"])
    state["martingale_message_ids"] = []

    # enviar entrada principal
    msg_id = await send_message(main_entry_text(color))
    if msg_id:
        state["entrada_message_id"] = msg_id
        state["waiting_for_result"] = True
        state["last_signal_color"] = color
        state["martingale_count"] = 0  # Reset martingale count para nova entrada
        state["last_signal_pattern_id"] = pat["id"]
        state["last_signal_sequence"] = current_sequence
        state["last_signal_round_id"] = state["last_round_id"]  # Guardar o round em que o sinal foi enviado
        log.info("✅ Enviado sinal principal: %s (msg_id=%s, padrão=%s, round=%s)", color, msg_id, pat["id"], state["last_round_id"])


# ---------- Rotinas assíncronas ----------
async def api_worker():
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await update_history_from_api(session)
                # cada vez que atualizamos history, tentamos resolver sinais pendentes
                await resolve_after_result()
            except Exception as e:
                log.error(f"❌ Erro no api_worker: {e}")
                await asyncio.sleep(5)
            await asyncio.sleep(API_POLL_INTERVAL)


async def scheduler_worker():
    # controla o ciclo de sinais (a cada SIGNAL_CYCLE_INTERVAL tenta enviar um sinal)
    await asyncio.sleep(2)
    while True:
        try:
            await try_send_signal()
        except Exception as e:
            log.error(f"❌ Erro no scheduler_worker: {e}")
            await asyncio.sleep(2)
        await asyncio.sleep(SIGNAL_CYCLE_INTERVAL)
