"""Bot de lista de divulgação + catálogo de canais e grupos do Telegram, todo por botões.

Para quem usa: categorias, populares, recentes, busca, "meus canais" e modo inline.
Para o dono (ADMIN_ID): painel com publicar agora, horários, dias, colunas, links avulsos,
fixar, proteger, mídia, moderação, bloqueio, verificação, anúncio e exportação de usuários.
"""

import asyncio
import csv
import html
import io
import json
import logging
import os
import random
import sqlite3
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultsButton,
    InputTextMessageContent,
    MenuButtonWebApp,
    Update,
    WebAppInfo,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

PASTA = Path(__file__).parent
TZ = timezone(timedelta(hours=-4))  # Manaus, sem horário de verão
POR_PAGINA = 8
MAX_LINKS = 8  # links avulsos na lista

CATEGORIAS = [
    "📰 Notícias", "😂 Humor", "🎬 Filmes e Séries", "🎵 Música", "⚽ Esportes",
    "📚 Educação", "💼 Negócios", "💻 Tecnologia", "🛍️ Ofertas", "🏠 Imóveis",
    "🎮 Games", "📦 Outros",
]
DIAS = ["Dom", "Seg", "Ter", "Qua", "Qui", "Sex", "Sáb"]  # convenção do PTB: 0 = domingo
ACEITA = {"canais": "só canais", "grupos": "só grupos", "ambos": "canais e grupos"}
CORES = {"nenhuma": "⚪ Sem cor", "azul": "🔵 Azul", "verde": "🟢 Verde", "vermelho": "🔴 Vermelho",
         "xadrez": "🏁 Xadrez", "faixas": "🌈 Faixas", "sorteio": "🎲 Sorteio"}
ORDEM = {"rodizio": "🔄 Rodízio", "membros": "👥 Por membros", "aleatoria": "🎲 Aleatória"}


def carregar_env() -> None:
    arq = PASTA / ".env"
    if not arq.exists():
        return
    for linha in arq.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha and not linha.startswith("#") and "=" in linha:
            k, v = linha.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


carregar_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0") or 0)
API_ID = int(os.environ.get("TG_API_ID", "0") or 0)    # busca global (opcional)
API_HASH = os.environ.get("TG_API_HASH", "")
SESSAO_BUSCA = PASTA / "busca"                         # criada pelo login_busca.py
# Versão web da lista (site + Mini App). O bot grava docs/canais.json; o GitHub Pages serve.
WEB_URL = os.environ.get("WEB_URL", "https://manoelmigueis.github.io/bot-lista-canais/")
ARQ_SITE = PASTA / "docs" / "canais.json"

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("lista")

# ================================================================ banco

db = sqlite3.connect(PASTA / "lista.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS config (chave TEXT PRIMARY KEY, valor TEXT);
    CREATE TABLE IF NOT EXISTS canais (
        chat_id INTEGER PRIMARY KEY, titulo TEXT, link TEXT, membros INTEGER,
        dono_id INTEGER, ativo INTEGER DEFAULT 1, ultimo_envio TEXT);
    CREATE TABLE IF NOT EXISTS envios (chat_id INTEGER, message_id INTEGER, apagar_em TEXT);
    CREATE TABLE IF NOT EXISTS usuarios (user_id INTEGER PRIMARY KEY, nome TEXT, username TEXT, criado_em TEXT);
    CREATE TABLE IF NOT EXISTS links (id INTEGER PRIMARY KEY AUTOINCREMENT, texto TEXT, url TEXT,
        posicao TEXT DEFAULT 'fim');
    """
)
_cols = {r[1] for r in db.execute("PRAGMA table_info(canais)")}
if "teclado" not in {r[1] for r in db.execute("PRAGMA table_info(envios)")}:
    db.execute("ALTER TABLE envios ADD COLUMN teclado TEXT")
for _col, _ddl in {
    "categoria": "TEXT DEFAULT '📦 Outros'",
    "criado_em": "TEXT",
    "aprovado": "INTEGER DEFAULT 1",
    "tipo": "TEXT DEFAULT 'channel'",
    "bloqueado": "INTEGER DEFAULT 0",
}.items():
    if _col not in _cols:
        db.execute(f"ALTER TABLE canais ADD COLUMN {_col} {_ddl}")
# chaves antigas -> novas
for _velha, _nova in (("horario", "horarios"), ("foto", "midia")):
    _v = db.execute("SELECT valor FROM config WHERE chave=?", (_velha,)).fetchone()
    if _v and not db.execute("SELECT 1 FROM config WHERE chave=?", (_nova,)).fetchone():
        db.execute("INSERT INTO config VALUES (?,?)", (_nova, _v[0]))
        if _nova == "midia" and _v[0]:
            db.execute("INSERT OR REPLACE INTO config VALUES ('midia_tipo','photo')")
db.commit()

PADRAO = {
    "descricao": "🔥 Lista de canais\n\n🚀 Adicione seu canal na lista, é grátis e automático!",
    "midia": "",
    "midia_tipo": "",
    "horarios": "20:00",
    "dias": "0123456",
    "minimo": "100",
    "maximo": "30",
    "horas_apagar": "12",
    "moderacao": "0",
    "aceita": "canais",
    "colunas": "1",
    "cores": "nenhuma",
    "ordem": "rodizio",
    "fixar": "0",
    "proteger": "0",
    "ver_mais": "1",
    "avisos": "1",
    "inicio": "",
    "suporte": "",
}
NA_LISTA = "ativo=1 AND aprovado=1 AND bloqueado=0"


def cfg(chave: str) -> str:
    row = db.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
    return row["valor"] if row else PADRAO[chave]


def set_cfg(chave: str, valor: str) -> None:
    db.execute("INSERT OR REPLACE INTO config VALUES (?,?)", (chave, valor))
    db.commit()


def ligado(chave: str) -> bool:
    return cfg(chave) == "1"


def canal(chat_id: int):
    return db.execute("SELECT * FROM canais WHERE chat_id=?", (chat_id,)).fetchone()


def agora() -> datetime:
    return datetime.now(TZ)


def contar(sql: str, *args) -> int:
    return db.execute(sql, args).fetchone()[0]


# ================================================================ utilidades de tela


def B(texto: str, dado: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(texto, callback_data=dado)


def U(texto: str, url: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(texto, url=url)


def kb(*linhas) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(l) for l in linhas if l])


def esc(t) -> str:
    return html.escape(str(t or ""))


def num(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)


def sim(chave: str) -> str:
    return "Sim" if ligado(chave) else "Não"


def eh_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id == ADMIN_ID


async def tela(update: Update, texto: str, teclado: InlineKeyboardMarkup | None = None) -> None:
    """Mostra uma tela: edita a mensagem do botão tocado ou manda uma nova."""
    q = update.callback_query
    if q and q.message and q.message.text:
        try:
            await q.edit_message_text(texto, reply_markup=teclado, parse_mode=ParseMode.HTML,
                                      disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e):
                return
    await update.effective_chat.send_message(texto, reply_markup=teclado, parse_mode=ParseMode.HTML,
                                             disable_web_page_preview=True)


async def avisar(bot, user_id: int | None, texto: str, teclado=None) -> None:
    if not user_id:
        return
    try:
        await bot.send_message(user_id, texto, reply_markup=teclado, parse_mode=ParseMode.HTML)
    except TelegramError:
        pass  # a pessoa nunca deu /start no bot


async def avisar_admin(bot, texto: str, teclado=None, forcar: bool = False) -> None:
    if forcar or ligado("avisos"):
        await avisar(bot, ADMIN_ID, texto, teclado)


def paginar(rows, pagina: int, prefixo: str, voltar: str, rotulo, dado=None):
    """Monta botões de uma página de canais + navegação."""
    total = len(rows)
    paginas = max(1, -(-total // POR_PAGINA))
    pagina = max(0, min(pagina, paginas - 1))
    fatia = rows[pagina * POR_PAGINA:(pagina + 1) * POR_PAGINA]
    linhas = []
    for r in fatia:
        linhas.append([B(rotulo(r), dado(r))] if dado else [U(rotulo(r), r["link"])])
    nav = []
    if pagina > 0:
        nav.append(B("◀️", f"{prefixo}:{pagina - 1}"))
    if paginas > 1:
        nav.append(B(f"{pagina + 1}/{paginas}", "nada"))
    if pagina < paginas - 1:
        nav.append(B("▶️", f"{prefixo}:{pagina + 1}"))
    if nav:
        linhas.append(nav)
    linhas.append([B("⬅️ Voltar", voltar), B("🏠 Início", "menu")])
    return InlineKeyboardMarkup(linhas)


def icone(r) -> str:
    return "👥" if r["tipo"] != ChatType.CHANNEL else "📢"


def rotulo_canal(r) -> str:
    return f"{r['titulo'][:40]} · 👥 {num(r['membros'])}"


# ================================================================ registro de canais e usuários


async def registrar_usuario(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    u, chat = update.effective_user, update.effective_chat
    if u and chat and chat.type == ChatType.PRIVATE:
        db.execute(
            """INSERT INTO usuarios VALUES (?,?,?,?) ON CONFLICT(user_id)
               DO UPDATE SET nome=excluded.nome, username=excluded.username""",
            (u.id, u.full_name, u.username, agora().isoformat()),
        )
        db.commit()


async def link_do_canal(bot, chat_id: int) -> str | None:
    chat = await bot.get_chat(chat_id)
    if chat.username:
        return f"https://t.me/{chat.username}"
    if chat.invite_link:
        return chat.invite_link
    try:
        return (await bot.create_chat_invite_link(chat_id)).invite_link
    except TelegramError:
        return None


def aceita_tipo(tipo: str) -> bool:
    a = cfg("aceita")
    return a == "ambos" or (a == "canais") == (tipo == ChatType.CHANNEL)


async def registrar(bot, chat_id: int, dono_id: int | None) -> tuple[bool, str]:
    """Valida e grava o canal/grupo. Devolve (deu_certo, mensagem para o dono)."""
    try:
        chat = await bot.get_chat(chat_id)
        eu = await bot.get_chat_member(chat_id, bot.id)
        membros = await bot.get_chat_member_count(chat_id)
    except TelegramError as e:
        return False, f"❌ Não consegui acessar: {esc(e.message)}"

    antigo = canal(chat_id)
    if antigo and antigo["bloqueado"]:
        return False, f"⛔ <b>{esc(chat.title)}</b> está bloqueado e não pode entrar na lista."
    if not aceita_tipo(chat.type):
        return False, f"❌ No momento a lista aceita {ACEITA[cfg('aceita')]}."
    if eu.status != ChatMemberStatus.ADMINISTRATOR:
        return False, "❌ Me coloque como administrador primeiro."
    if chat.type == ChatType.CHANNEL and not (eu.can_post_messages and eu.can_delete_messages):
        return False, "❌ Preciso das permissões de <b>publicar</b> e <b>apagar</b> mensagens no canal."

    minimo = int(cfg("minimo"))
    if membros < minimo:
        return False, (f"❌ <b>{esc(chat.title)}</b> tem {membros} membros. "
                       f"O mínimo para entrar na lista é {minimo}.")

    link = await link_do_canal(bot, chat_id)
    if not link:
        return False, "❌ Não consegui um link. Me dê a permissão de <b>convidar usuários</b>."

    aprovado = 0 if ligado("moderacao") else 1
    db.execute(
        """INSERT INTO canais (chat_id, titulo, link, membros, dono_id, ativo, criado_em, aprovado, tipo)
           VALUES (?,?,?,?,?,1,?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET titulo=excluded.titulo, link=excluded.link,
           membros=excluded.membros, tipo=excluded.tipo,
           dono_id=COALESCE(excluded.dono_id, canais.dono_id), ativo=1""",
        (chat_id, chat.title, link, membros, dono_id, agora().isoformat(), aprovado, chat.type),
    )
    db.commit()
    r = canal(chat_id)

    if r["aprovado"]:
        if not antigo:
            await avisar_admin(bot, f"🆕 {icone(r)} <b>{esc(r['titulo'])}</b> entrou na lista "
                                    f"({num(membros)} membros).")
        return True, (f"✅ <b>{esc(chat.title)}</b> entrou na lista!\n"
                      f"Ela sai nos horários: {cfg('horarios')}.")
    if not antigo:
        await avisar_admin(bot, tela_pendente_texto(r), teclado_pendente(chat_id), forcar=True)
    return True, f"⏳ <b>{esc(chat.title)}</b> foi recebido e está aguardando aprovação."


async def pos_registro(bot, chat_id: int, dono_id: int | None, ok: bool, msg: str) -> None:
    """Avisa o dono e, se deu certo, pede a categoria."""
    if ok:
        await avisar(bot, dono_id, msg + "\n\n📂 Escolha a categoria:", teclado_categorias(chat_id))
    else:
        await avisar(bot, dono_id, msg)


def teclado_categorias(chat_id: int) -> InlineKeyboardMarkup:
    botoes = [B(c, f"pc:{chat_id}:{i}") for i, c in enumerate(CATEGORIAS)]
    return InlineKeyboardMarkup([botoes[i:i + 2] for i in range(0, len(botoes), 2)])


async def mudou_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mcm = update.my_chat_member
    if mcm.chat.type not in (ChatType.CHANNEL, ChatType.SUPERGROUP, ChatType.GROUP):
        return
    novo = mcm.new_chat_member.status
    quem = mcm.from_user.id if mcm.from_user else None

    if novo == ChatMemberStatus.ADMINISTRATOR:
        ok, msg = await registrar(context.bot, mcm.chat.id, quem)
        await pos_registro(context.bot, mcm.chat.id, quem, ok, msg)
    elif novo in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.MEMBER):
        await tirar_da_lista(context.bot, mcm.chat.id,
                             "o bot foi tirado da administração do canal.",
                             "me tirou da administração.")


# ================================================================ telas de quem usa


def botoes_adicionar(bot) -> list:
    base = f"https://t.me/{bot.username}"
    canal_url = (f"{base}?startchannel&admin=post_messages+delete_messages"
                 "+edit_messages+invite_users")
    grupo_url = f"{base}?startgroup&admin=delete_messages+pin_messages+invite_users"
    a = cfg("aceita")
    linha = []
    if a in ("canais", "ambos"):
        linha.append(U("➕ Adicionar meu canal", canal_url))
    if a in ("grupos", "ambos"):
        linha.append(U("➕ Adicionar meu grupo", grupo_url))
    return linha


def url_suporte() -> str | None:
    s = cfg("suporte").strip()
    if not s:
        return None
    return f"https://t.me/{s[1:]}" if s.startswith("@") else s


async def menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("esperando", None)
    total, membros = db.execute(
        f"SELECT COUNT(*), COALESCE(SUM(membros),0) FROM canais WHERE {NA_LISTA}").fetchone()
    resumo = (f"📊 <b>{total}</b> na lista · <b>{num(membros)}</b> membros somados\n"
              f"🕗 A lista sai às <b>{cfg('horarios')}</b>")
    if cfg("inicio"):
        texto = f"{esc(cfg('inicio'))}\n\n{resumo}"
    else:
        texto = (f"🌍 <b>{esc(context.bot.first_name)}</b>\n\n"
                 "Descubra canais e divulgue o seu de graça.\n\n" + resumo)
    sup = url_suporte()
    teclado = kb(
        botoes_adicionar(context.bot),
        [InlineKeyboardButton("🌐 Ver a lista completa", web_app=WebAppInfo(WEB_URL))],
        [B("📂 Categorias", "cats"), B("🔥 Populares", "pop:0")],
        [B("🆕 Recentes", "rec:0"), B("🔍 Buscar", "busca")],
        [B("📋 Meus canais", "meus"), B("ℹ️ Como funciona", "ajuda")],
        [U("📞 Suporte", sup)] if sup else None,
        [B("⚙️ Painel do dono", "adm")] if eh_admin(update) else None,
    )
    await tela(update, texto, teclado)


async def ajuda(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    texto = (
        "ℹ️ <b>Como funciona</b>\n\n"
        "1️⃣ Toque em <b>➕ Adicionar</b> e escolha o seu canal ou grupo.\n"
        "2️⃣ Confirme as permissões que o bot pede.\n"
        "3️⃣ Pronto! Você recebe a confirmação aqui e escolhe a categoria.\n\n"
        f"✅ Aceitamos: <b>{ACEITA[cfg('aceita')]}</b>\n"
        f"👥 Mínimo de membros: <b>{cfg('minimo')}</b>\n"
        f"🕗 A lista sai às <b>{cfg('horarios')}</b> ({texto_dias()}) "
        f"e some depois de <b>{cfg('horas_apagar')} h</b>.\n"
        "🔄 Se houver muitos canais, eles se revezam, para todo mundo aparecer.\n\n"
        "🚪 Para sair, use <b>📋 Meus canais</b> ou me tire do canal.\n"
        f"💡 Em qualquer conversa, digite <code>@{esc(context.bot.username)} nome</code> "
        "para buscar canais."
    )
    await tela(update, texto, kb([B("🏠 Início", "menu")]))


async def categorias(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    contagem = dict(db.execute(
        f"SELECT categoria, COUNT(*) FROM canais WHERE {NA_LISTA} GROUP BY categoria").fetchall())
    botoes = [B(f"{c} ({contagem.get(c, 0)})", f"cat:{i}:0") for i, c in enumerate(CATEGORIAS)]
    linhas = [botoes[i:i + 2] for i in range(0, len(botoes), 2)]
    linhas.append([B("🏠 Início", "menu")])
    await tela(update, "📂 <b>Categorias</b>\n\nEscolha uma:", InlineKeyboardMarkup(linhas))


async def lista_categoria(update: Update, i: int, pagina: int) -> None:
    nome = CATEGORIAS[i]
    rows = db.execute(
        f"SELECT * FROM canais WHERE {NA_LISTA} AND categoria=? ORDER BY membros DESC",
        (nome,)).fetchall()
    texto = f"{nome}\n\n" + (f"{len(rows)} na lista. Toque para entrar:" if rows
                             else "Nada nesta categoria ainda.")
    await tela(update, texto, paginar(rows, pagina, f"cat:{i}", "cats", rotulo_canal))


async def populares(update: Update, pagina: int) -> None:
    rows = db.execute(f"SELECT * FROM canais WHERE {NA_LISTA} ORDER BY membros DESC").fetchall()
    texto = "🔥 <b>Populares</b>\n\n" + ("Os que têm mais membros:" if rows else "Nada ainda.")
    await tela(update, texto, paginar(rows, pagina, "pop", "menu", rotulo_canal))


async def recentes(update: Update, pagina: int) -> None:
    rows = db.execute(f"SELECT * FROM canais WHERE {NA_LISTA} ORDER BY criado_em DESC").fetchall()
    texto = "🆕 <b>Recentes</b>\n\n" + ("Os últimos que entraram:" if rows else "Nada ainda.")
    await tela(update, texto, paginar(rows, pagina, "rec", "menu", rotulo_canal))


async def pedir_busca(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["esperando"] = "busca"
    await tela(update, "🔍 <b>Buscar</b>\n\nMande o nome (ou parte do nome):",
               kb([B("✖️ Cancelar", "menu")]))


async def resultado_busca(update: Update, termo: str) -> None:
    rows = db.execute(
        f"SELECT * FROM canais WHERE {NA_LISTA} AND titulo LIKE ? ORDER BY membros DESC LIMIT 40",
        (f"%{termo}%",)).fetchall()
    texto = (f"🔍 Resultados para <b>{esc(termo)}</b>: {len(rows)}" if rows
             else f"🔍 Nada encontrado para <b>{esc(termo)}</b>.")
    linhas = [[U(rotulo_canal(r), r["link"])] for r in rows[:POR_PAGINA]]
    linhas.append([B("🔍 Nova busca", "busca"), B("🏠 Início", "menu")])
    await tela(update, texto, InlineKeyboardMarkup(linhas))


# ---------------------------------------------------------------- meus canais


def status_de(r) -> str:
    if r["bloqueado"]:
        return "⛔ Bloqueado"
    if r["ativo"] and r["aprovado"]:
        return "✅ Na lista"
    if r["ativo"]:
        return "⏳ Aguardando aprovação"
    return "⏸️ Fora da lista"


async def meus_canais(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.execute("SELECT * FROM canais WHERE dono_id=? ORDER BY titulo",
                      (update.effective_user.id,)).fetchall()
    if not rows:
        await tela(update, "📋 <b>Meus canais</b>\n\nVocê ainda não tem nada aqui.\n"
                           "Toque em ➕ para adicionar.",
                   kb(botoes_adicionar(context.bot), [B("🏠 Início", "menu")]))
        return
    linhas = [[B(f"{status_de(r)[:1]} {r['titulo'][:40]}", f"meu:{r['chat_id']}")] for r in rows]
    linhas.append(botoes_adicionar(context.bot))
    linhas.append([B("🏠 Início", "menu")])
    await tela(update, "📋 <b>Meus canais</b>\n\nToque para gerenciar:", InlineKeyboardMarkup(linhas))


def dono_ou_admin(update: Update, r) -> bool:
    return r is not None and (r["dono_id"] == update.effective_user.id or eh_admin(update))


def ficha(r) -> str:
    ultimo = r["ultimo_envio"][:16].replace("T", " ") if r["ultimo_envio"] else "ainda não"
    return (f"{icone(r)} <b>{esc(r['titulo'])}</b>\n\n"
            f"Status: {status_de(r)}\n"
            f"Categoria: {esc(r['categoria'])}\n"
            f"Membros: {num(r['membros'])}\n"
            f"Última vez na lista: {ultimo}")


async def meu_canal(update: Update, chat_id: int) -> None:
    r = canal(chat_id)
    if not dono_ou_admin(update, r):
        await tela(update, "❌ Não encontrado.", kb([B("📋 Meus canais", "meus")]))
        return
    if r["bloqueado"]:
        acao = None
    elif r["ativo"]:
        acao = [B("🚪 Sair da lista", f"sair:{chat_id}")]
    else:
        acao = [B("↩️ Voltar para a lista", f"volta:{chat_id}")]
    await tela(update, ficha(r), kb(
        [U("🔗 Abrir", r["link"])],
        [B("📂 Mudar categoria", f"setcat:{chat_id}")],
        acao,
        [B("⬅️ Voltar", "meus"), B("🏠 Início", "menu")],
    ))


# ================================================================ painel do dono


def texto_dias() -> str:
    d = cfg("dias")
    if len(d) == 7:
        return "todos os dias"
    return ", ".join(DIAS[int(i)] for i in sorted(d)) or "nenhum dia"


async def painel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("esperando", None)
    pend = contar("SELECT COUNT(*) FROM canais WHERE ativo=1 AND aprovado=0 AND bloqueado=0")
    n_canais = contar(f"SELECT COUNT(*) FROM canais WHERE {NA_LISTA} AND tipo='channel'")
    n_grupos = contar(f"SELECT COUNT(*) FROM canais WHERE {NA_LISTA} AND tipo!='channel'")
    texto = (
        "⚙️ <b>Painel do dono</b>\n\n"
        f"👤 Usuários: {contar('SELECT COUNT(*) FROM usuarios')}\n"
        f"📢 Canais aprovados: {n_canais} · 👥 Grupos: {n_grupos}\n"
        f"⏳ Aguardando aprovação: {pend}\n\n"
        f"🧭 Aceita: {ACEITA[cfg('aceita')]}\n"
        f"👥 Mínimo de inscritos: {cfg('minimo')}\n"
        f"🔢 Botões por lista: {cfg('maximo')} · ▦ Colunas: {cfg('colunas')}\n"
        f"🔀 Organização: {ORDEM[cfg('ordem')]}\n"
        f"🕒 Horários: {cfg('horarios')}\n"
        f"📅 Dias: {texto_dias()}\n"
        f"📌 Fixar: {sim('fixar')} · 🔒 Proteger: {sim('proteger')}\n"
        f"🗑️ Apaga após: {cfg('horas_apagar')} h"
    )
    await tela(update, texto, kb(
        [B("📤 Publicar lista agora", "pub")],
        [B(f"✅ Aprovar canais e grupos ({pend})", "pend")],
        [B(f"🧭 Aceita: {ACEITA[cfg('aceita')]}", "cy:aceita")],
        [B("🕒 Horários", "cfg:horarios"), B("📅 Dias da semana", "dias")],
        [B("👥 Mín. inscritos", "cfg:minimo"), B("🔢 Botões por lista", "cfg:maximo")],
        [B(f"▦ Colunas: {cfg('colunas')}", "cy:colunas"), B(f"🔀 {ORDEM[cfg('ordem')]}", "cy:ordem")],
        [B(f"📌 Fixar: {sim('fixar')}", "tg:fixar"), B(f"🔒 Proteger: {sim('proteger')}", "tg:proteger")],
        [B("📝 Texto da lista", "cfg:descricao"), B("🖼️ Mídia", "cfg:midia")],
        [B("🔗 Meus botões (links)", "bts"), B(f"📂 Ver mais: {sim('ver_mais')}", "tg:ver_mais")],
        [B(f"🎨 Cores: {CORES[cfg('cores')]}", "cy:cores")],
        [B("🏠 Página inicial", "cfg:inicio"), B(f"📞 Suporte: {'ligado' if cfg('suporte') else 'desligado'}", "cfg:suporte")],
        [B(f"🗑️ Apagar após: {cfg('horas_apagar')} h", "cfg:horas_apagar"), B(f"💬 Avisos: {sim('avisos')}", "tg:avisos")],
        [B(f"🛡️ Aprovação manual: {sim('moderacao')}", "tg:moderacao")],
        [B("📢 Canais do bot", "lst:c:0"), B("👥 Grupos do bot", "lst:g:0")],
        [B("🔄 Verificar links e nomes", "verif")],
        [B("👁️ Prévia da lista", "previa"), B("🧹 Excluir listas postadas", "limp")],
        [B("📣 Enviar anúncio", "anun"), B("📄 Exportar usuários", "exp")],
        [B("📊 Estatísticas", "stats"), B("⬅️ Menu inicial", "menu")],
    ))


CAMPOS = {
    "horarios": ("🕒 Horários", "os horários separados por vírgula, por exemplo <b>08:00, 20:00</b> (até 6)"),
    "minimo": ("👥 Mínimo de inscritos", "o mínimo de membros (ex: <b>100</b>)"),
    "maximo": ("🔢 Botões por lista", "quantos canais cabem na lista, de 2 a 90 (ex: <b>30</b>)"),
    "horas_apagar": ("🗑️ Apagar após", "depois de quantas horas a lista some, de 1 a 48 (ex: <b>12</b>)"),
    "descricao": ("📝 Texto da lista", "o novo texto da lista (até 900 caracteres)"),
    "midia": ("🖼️ Mídia", "uma <b>foto</b>, <b>vídeo</b> ou <b>GIF</b> para ir junto da lista"),
    "inicio": ("🏠 Página inicial", "o texto de boas-vindas do /start (até 800 caracteres). "
                                   "Mande <b>-</b> para voltar ao padrão"),
    "suporte": ("📞 Suporte", "o @ ou link de contato do suporte. Mande <b>-</b> para desligar"),
}


async def pedir_config(update: Update, context: ContextTypes.DEFAULT_TYPE, chave: str) -> None:
    context.user_data["esperando"] = f"cfg:{chave}"
    nome, pedido = CAMPOS[chave]
    atual = cfg(chave)
    extra = None
    if chave == "midia" and atual:
        extra = [B("🚫 Remover mídia", "semmidia")]
    elif chave in ("descricao", "inicio") and atual:
        pedido += f"\n\nAtual:\n<i>{esc(atual)}</i>"
    elif chave not in ("midia",):
        pedido += f"\n\nAtual: <b>{esc(atual) or '—'}</b>"
    await tela(update, f"{nome}\n\nMande {pedido}", kb(extra, [B("✖️ Cancelar", "adm")]))


def validar_config(chave: str, texto: str) -> tuple[str | None, str]:
    """Devolve (valor_ok, erro)."""
    texto = texto.strip()
    if chave == "horarios":
        hs = set()
        for parte in texto.replace(";", ",").replace(" ", ",").split(","):
            if not parte:
                continue
            try:
                h, m = map(int, parte.split(":"))
                dtime(h, m)
            except ValueError:
                return None, f"\"{parte}\" não é um horário. Use HH:MM, por exemplo 08:00, 20:00."
            hs.add(f"{h:02d}:{m:02d}")
        if not 1 <= len(hs) <= 6:
            return None, "Mande de 1 a 6 horários."
        return ", ".join(sorted(hs)), ""
    if chave == "descricao":
        if len(texto) > 900:
            return None, f"Texto com {len(texto)} caracteres. O limite é 900."
        return texto, ""
    if chave == "inicio":
        if texto == "-":
            return "", ""
        if len(texto) > 800:
            return None, f"Texto com {len(texto)} caracteres. O limite é 800."
        return texto, ""
    if chave == "suporte":
        if texto == "-":
            return "", ""
        if texto.startswith("@") and len(texto) > 3 and " " not in texto:
            return texto, ""
        if texto.startswith("https://"):
            return texto, ""
        return None, "Mande um @usuario ou um link começando com https://"
    if not texto.isdigit():
        return None, "Mande só um número, por exemplo 30."
    n = int(texto)
    if chave == "maximo" and not 2 <= n <= 90:
        return None, "Os botões por lista precisam ficar entre 2 e 90."
    if chave == "horas_apagar" and not 1 <= n <= 48:
        return None, "Escolha entre 1 e 48 horas."
    return str(n), ""


async def tela_dias(update: Update) -> None:
    d = cfg("dias")
    botoes = [B(f"{'✅' if str(i) in d else '▫️'} {nome}", f"dia:{i}") for i, nome in enumerate(DIAS)]
    await tela(update, f"📅 <b>Dias de postagem</b>\n\nHoje: {texto_dias()}.\nToque para ligar ou desligar:",
               InlineKeyboardMarkup([botoes[:4], botoes[4:], [B("⬅️ Painel", "adm")]]))


# ---------------------------------------------------------------- links avulsos


async def tela_links(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("esperando", None)
    rows = db.execute("SELECT * FROM links ORDER BY posicao DESC, id").fetchall()
    texto = ("🔗 <b>Meus botões</b>\n\nLinks avulsos que entram em toda lista "
             "(no topo ou no fim). Só você mexe aqui.\n\n"
             + (f"{len(rows)} de {MAX_LINKS} botões." if rows else "Nenhum botão ainda."))
    linhas = [[B(f"{'⬆️' if r['posicao'] == 'topo' else '⬇️'} {r['texto'][:40]}", f"bt:{r['id']}")]
              for r in rows]
    if len(rows) < MAX_LINKS:
        linhas.append([B("➕ Novo botão", "btnovo")])
    linhas.append([B("⬅️ Painel", "adm")])
    await tela(update, texto, InlineKeyboardMarkup(linhas))


async def tela_link(update: Update, link_id: int) -> None:
    r = db.execute("SELECT * FROM links WHERE id=?", (link_id,)).fetchone()
    if not r:
        await tela(update, "Botão não encontrado.", kb([B("⬅️ Meus botões", "bts")]))
        return
    pos = "no topo" if r["posicao"] == "topo" else "no fim"
    await tela(update, f"🔗 <b>{esc(r['texto'])}</b>\n{esc(r['url'])}\n\nAparece {pos} da lista.", kb(
        [U("🔗 Testar link", r["url"])],
        [B("⬆️ Mover para o topo" if r["posicao"] != "topo" else "⬇️ Mover para o fim", f"btpos:{link_id}")],
        [B("🗑️ Apagar botão", f"btdel:{link_id}")],
        [B("⬅️ Meus botões", "bts")],
    ))


def validar_link(texto: str) -> tuple[tuple[str, str] | None, str]:
    if "|" not in texto:
        return None, "Use o formato: Texto do botão | https://link"
    rotulo, url = (p.strip() for p in texto.split("|", 1))
    if not rotulo or len(rotulo) > 60:
        return None, "O texto do botão precisa ter de 1 a 60 caracteres."
    if url.startswith("@"):
        url = f"https://t.me/{url[1:]}"
    if not url.startswith(("https://", "http://", "tg://")):
        return None, "O link precisa começar com https://"
    return (rotulo, url), ""


# ---------------------------------------------------------------- moderação e canais


def tela_pendente_texto(r) -> str:
    return (
        f"⏳ <b>Aguardando aprovação</b>\n\n"
        f"{icone(r)} {esc(r['titulo'])}\n"
        f"👥 {num(r['membros'])} membros\n"
        f"📂 {esc(r['categoria'])}\n"
        f"🔗 {esc(r['link'])}"
    )


def teclado_pendente(chat_id: int) -> InlineKeyboardMarkup:
    return kb([B("✅ Aprovar", f"ap:{chat_id}"), B("❌ Rejeitar", f"rj:{chat_id}")],
              [B("⬅️ Painel", "adm")])


async def pendentes(update: Update) -> None:
    rows = db.execute("SELECT * FROM canais WHERE ativo=1 AND aprovado=0 AND bloqueado=0 "
                      "ORDER BY criado_em").fetchall()
    if not rows:
        await tela(update, "⏳ Nada aguardando aprovação. 🎉", kb([B("⬅️ Painel", "adm")]))
        return
    r = rows[0]
    await tela(update, tela_pendente_texto(r) + f"\n\n<i>{len(rows)} na fila</i>", teclado_pendente(r["chat_id"]))


async def lista_admin(update: Update, tipo: str, pagina: int) -> None:
    filtro = "tipo='channel'" if tipo == "c" else "tipo!='channel'"
    rows = db.execute(f"SELECT * FROM canais WHERE {filtro} "
                      "ORDER BY bloqueado, ativo DESC, membros DESC").fetchall()
    nome = "📢 Canais do bot" if tipo == "c" else "👥 Grupos do bot"
    texto = f"{nome} ({len(rows)})\n\nToque em um para remover ou bloquear:"
    await tela(update, texto, paginar(
        rows, pagina, f"lst:{tipo}", "adm",
        lambda r: f"{status_de(r)[:1]} {rotulo_canal(r)}",
        lambda r: f"ca:{r['chat_id']}",
    ))


async def canal_admin(update: Update, chat_id: int) -> None:
    r = canal(chat_id)
    if not r:
        await tela(update, "Não encontrado.", kb([B("⬅️ Painel", "adm")]))
        return
    volta = "lst:c:0" if r["tipo"] == ChatType.CHANNEL else "lst:g:0"
    await tela(update, ficha(r), kb(
        [U("🔗 Abrir", r["link"])],
        [B("🗑️ Remover da lista", f"rm:{chat_id}")] if r["ativo"] else None,
        [B("✅ Desbloquear", f"dbl:{chat_id}") if r["bloqueado"] else B("⛔ Bloquear", f"bl:{chat_id}")],
        [B("📂 Mudar categoria", f"setcat:{chat_id}")],
        [B("⬅️ Voltar", volta)],
    ))


async def verificar(bot) -> str:
    rows = db.execute("SELECT * FROM canais WHERE ativo=1").fetchall()
    nomes, links, saiu = 0, 0, []
    for r in rows:
        try:
            chat = await bot.get_chat(r["chat_id"])
            eu = await bot.get_chat_member(r["chat_id"], bot.id)
            membros = await bot.get_chat_member_count(r["chat_id"])
        except TelegramError:
            eu = None
        if eu is None or eu.status != ChatMemberStatus.ADMINISTRATOR:
            db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (r["chat_id"],))
            saiu.append(r["titulo"])
            continue
        link = await link_do_canal(bot, r["chat_id"]) or r["link"]
        nomes += chat.title != r["titulo"]
        links += link != r["link"]
        db.execute("UPDATE canais SET titulo=?, link=?, membros=? WHERE chat_id=?",
                   (chat.title, link, membros, r["chat_id"]))
        await asyncio.sleep(0.05)
    db.commit()
    texto = (f"🔄 <b>Verificação concluída</b>\n\n"
             f"Conferidos: {len(rows)}\nNomes atualizados: {nomes}\nLinks atualizados: {links}\n"
             f"Saíram da lista (não sou mais admin): {len(saiu)}")
    if saiu:
        texto += "\n" + esc(", ".join(saiu[:20]))
    return texto


async def estatisticas(update: Update) -> None:
    total, membros = db.execute(
        f"SELECT COUNT(*), COALESCE(SUM(membros),0) FROM canais WHERE {NA_LISTA}").fetchone()
    por_cat = db.execute(
        f"SELECT categoria, COUNT(*) n FROM canais WHERE {NA_LISTA} GROUP BY categoria ORDER BY n DESC"
    ).fetchall()
    maior = db.execute(f"SELECT titulo, membros FROM canais WHERE {NA_LISTA} "
                       "ORDER BY membros DESC LIMIT 1").fetchone()
    no_ar = contar("SELECT COUNT(*) FROM envios")
    texto = (
        "📊 <b>Estatísticas</b>\n\n"
        f"👤 Usuários do bot: <b>{contar('SELECT COUNT(*) FROM usuarios')}</b>\n"
        f"✅ Na lista: <b>{total}</b>\n"
        f"⏳ Pendentes: <b>{contar('SELECT COUNT(*) FROM canais WHERE ativo=1 AND aprovado=0 AND bloqueado=0')}</b>\n"
        f"⏸️ Fora da lista: <b>{contar('SELECT COUNT(*) FROM canais WHERE ativo=0 AND bloqueado=0')}</b>\n"
        f"⛔ Bloqueados: <b>{contar('SELECT COUNT(*) FROM canais WHERE bloqueado=1')}</b>\n"
        f"👥 Membros somados: <b>{num(membros)}</b>\n"
        f"📬 Listas no ar agora: <b>{no_ar}</b>\n"
    )
    if maior:
        texto += f"🏆 Maior: {esc(maior['titulo'])} ({num(maior['membros'])})\n"
    if por_cat:
        texto += "\n<b>Por categoria:</b>\n" + "\n".join(f"{esc(c)}: {n}" for c, n in por_cat)
    await tela(update, texto, kb([B("⬅️ Painel", "adm")]))


async def exportar(update: Update) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["user_id", "nome", "username", "desde"])
    for r in db.execute("SELECT * FROM usuarios ORDER BY criado_em"):
        w.writerow([r["user_id"], r["nome"], f"@{r['username']}" if r["username"] else "", r["criado_em"][:10]])
    dados = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
    await update.effective_chat.send_document(dados, filename=f"usuarios-{agora():%Y-%m-%d}.csv",
                                              caption="📄 Usuários do bot")


async def enviar_anuncio(bot, de_chat: int, msg_id: int) -> str:
    ids = [r[0] for r in db.execute("SELECT user_id FROM usuarios")]
    ok = 0
    for uid in ids:
        try:
            await bot.copy_message(uid, de_chat, msg_id)
            ok += 1
        except TelegramError:
            pass  # bloqueou o bot
        await asyncio.sleep(0.05)
    return f"📣 Anúncio entregue para <b>{ok}</b> de {len(ids)} usuários."


# ================================================================ roteador de botões


async def botoes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    dado = q.data or ""
    cmd, _, resto = dado.partition(":")
    args = resto.split(":") if resto else []
    bot = context.bot
    try:
        await q.answer()
    except TelegramError:
        pass

    # ----- telas públicas
    if cmd == "nada":
        return
    if cmd == "menu":
        await menu(update, context)
    elif cmd == "ajuda":
        await ajuda(update, context)
    elif cmd == "cats":
        await categorias(update, context)
    elif cmd == "cat":
        await lista_categoria(update, int(args[0]), int(args[1]))
    elif cmd == "pop":
        await populares(update, int(args[0]))
    elif cmd == "rec":
        await recentes(update, int(args[0]))
    elif cmd == "busca":
        await pedir_busca(update, context)
    elif cmd == "meus":
        await meus_canais(update, context)
    elif cmd == "meu":
        await meu_canal(update, int(args[0]))

    # ----- ações do dono do canal
    elif cmd in ("setcat", "pc", "sair", "volta"):
        chat_id = int(args[0])
        r = canal(chat_id)
        if not dono_ou_admin(update, r):
            await tela(update, "❌ Só o dono do canal pode fazer isso.", kb([B("🏠 Início", "menu")]))
        elif cmd == "setcat":
            await tela(update, f"📂 Categoria de <b>{esc(r['titulo'])}</b>:", teclado_categorias(chat_id))
        elif cmd == "pc":
            db.execute("UPDATE canais SET categoria=? WHERE chat_id=?", (CATEGORIAS[int(args[1])], chat_id))
            db.commit()
            await meu_canal(update, chat_id)
        elif cmd == "sair":
            db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (chat_id,))
            db.commit()
            await meu_canal(update, chat_id)
        elif cmd == "volta":
            ok, msg = await registrar(bot, chat_id, r["dono_id"])
            if ok:
                await meu_canal(update, chat_id)
            else:
                await tela(update, msg, kb([B("⬅️ Voltar", f"meu:{chat_id}")]))

    # ----- daqui para baixo, só o dono do bot
    elif not eh_admin(update):
        await tela(update, "❌ Área só do dono do bot.", kb([B("🏠 Início", "menu")]))
    elif cmd == "adm":
        await painel(update, context)
    elif cmd == "cfg":
        await pedir_config(update, context, args[0])
    elif cmd == "tg":
        set_cfg(args[0], "0" if ligado(args[0]) else "1")
        await painel(update, context)
    elif cmd == "cy":
        opcoes = {"aceita": list(ACEITA), "colunas": ["1", "2", "3"], "ordem": list(ORDEM),
                  "cores": list(CORES)}[args[0]]
        atual = cfg(args[0])
        set_cfg(args[0], opcoes[(opcoes.index(atual) + 1) % len(opcoes)])
        await painel(update, context)
    elif cmd == "semmidia":
        set_cfg("midia", "")
        set_cfg("midia_tipo", "")
        await painel(update, context)
    elif cmd == "dias":
        await tela_dias(update)
    elif cmd == "dia":
        d = set(cfg("dias")) ^ {args[0]}
        if d:
            set_cfg("dias", "".join(sorted(d)))
            agendar(context.application)
        await tela_dias(update)

    # links avulsos
    elif cmd == "bts":
        await tela_links(update, context)
    elif cmd == "btnovo":
        context.user_data["esperando"] = "link"
        await tela(update, "🔗 <b>Novo botão</b>\n\nMande assim:\n"
                           "<code>Texto do botão | https://link</code>\n\n"
                           "Exemplo: <code>🏠 Apartamentos em Manaus | https://t.me/meucanal</code>",
                   kb([B("✖️ Cancelar", "bts")]))
    elif cmd == "bt":
        await tela_link(update, int(args[0]))
    elif cmd == "btpos":
        db.execute("UPDATE links SET posicao=CASE posicao WHEN 'topo' THEN 'fim' ELSE 'topo' END WHERE id=?",
                   (int(args[0]),))
        db.commit()
        await tela_link(update, int(args[0]))
    elif cmd == "btdel":
        db.execute("DELETE FROM links WHERE id=?", (int(args[0]),))
        db.commit()
        await tela_links(update, context)

    # moderação e canais
    elif cmd == "pend":
        await pendentes(update)
    elif cmd in ("ap", "rj"):
        chat_id = int(args[0])
        r = canal(chat_id)
        if r:
            if cmd == "ap":
                db.execute("UPDATE canais SET aprovado=1 WHERE chat_id=?", (chat_id,))
                await avisar(bot, r["dono_id"], f"✅ <b>{esc(r['titulo'])}</b> foi aprovado e já está na lista!")
            else:
                db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (chat_id,))
                await avisar(bot, r["dono_id"], f"❌ <b>{esc(r['titulo'])}</b> não foi aprovado para a lista.")
            db.commit()
        await pendentes(update)
    elif cmd == "lst":
        await lista_admin(update, args[0], int(args[1]))
    elif cmd == "ca":
        await canal_admin(update, int(args[0]))
    elif cmd in ("rm", "bl", "dbl"):
        chat_id = int(args[0])
        sql = {"rm": "ativo=0", "bl": "bloqueado=1, ativo=0", "dbl": "bloqueado=0, ativo=1"}[cmd]
        db.execute(f"UPDATE canais SET {sql} WHERE chat_id=?", (chat_id,))
        db.commit()
        await canal_admin(update, chat_id)
    elif cmd == "verif":
        await tela(update, "🔄 Verificando canais e grupos...")
        await tela(update, await verificar(bot), kb([B("⬅️ Painel", "adm")]))
    elif cmd == "stats":
        await estatisticas(update)

    # lista
    elif cmd == "pub":
        n = min(contar(f"SELECT COUNT(*) FROM canais WHERE {NA_LISTA}"), int(cfg("maximo")))
        await tela(update, f"📤 Publicar a lista agora em até <b>{n}</b> canais/grupos?\n\n"
                           "As listas que estão no ar serão apagadas antes.",
                   kb([B("✅ Publicar agora", "pubok"), B("✖️ Cancelar", "adm")]))
    elif cmd == "pubok":
        await tela(update, "⏳ Publicando a lista...")
        await tela(update, await fazer_envio(bot), kb([B("⬅️ Painel", "adm")]))
    elif cmd == "previa":
        await previa(update, context)
    elif cmd == "limp":
        await tela(update, f"🧹 Apagar as {contar('SELECT COUNT(*) FROM envios')} listas que estão no ar?",
                   kb([B("✅ Apagar", "limpok"), B("✖️ Cancelar", "adm")]))
    elif cmd == "limpok":
        n = await apagar_envios(bot, tudo=True)
        await tela(update, f"🧹 {n} listas apagadas.", kb([B("⬅️ Painel", "adm")]))

    # usuários
    elif cmd == "anun":
        context.user_data["esperando"] = "anuncio"
        await tela(update, f"📣 <b>Enviar anúncio</b>\n\nMande a mensagem (texto, foto, vídeo...) "
                           f"que vai para os {contar('SELECT COUNT(*) FROM usuarios')} usuários do bot. "
                           "Antes de enviar, eu peço confirmação.",
                   kb([B("✖️ Cancelar", "adm")]))
    elif cmd == "anok":
        rasc = context.user_data.pop("anuncio", None)
        if rasc:
            await tela(update, "⏳ Enviando o anúncio...")
            await tela(update, await enviar_anuncio(bot, *rasc), kb([B("⬅️ Painel", "adm")]))
        else:
            await painel(update, context)
    elif cmd == "exp":
        await exportar(update)


# ================================================================ mensagens no privado


async def mensagem_privada(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Texto ou mídia no privado: responde ao que o bot estava esperando."""
    esperando = context.user_data.get("esperando")
    msg = update.message
    texto = msg.text or ""

    if esperando == "busca" and texto:
        context.user_data.pop("esperando")
        await resultado_busca(update, texto.strip()[:50])
        return
    if not eh_admin(update) or not esperando:
        await menu(update, context)
        return

    if esperando == "anuncio":
        context.user_data.pop("esperando")
        context.user_data["anuncio"] = (msg.chat_id, msg.message_id)
        n = contar("SELECT COUNT(*) FROM usuarios")
        await msg.reply_text(f"👆 Enviar este anúncio para {n} usuários?",
                             reply_markup=kb([B("✅ Enviar", "anok"), B("✖️ Cancelar", "adm")]))
    elif esperando == "link":
        valor, erro = validar_link(texto)
        if erro:
            await msg.reply_text(f"❌ {erro}")
            return
        context.user_data.pop("esperando")
        db.execute("INSERT INTO links (texto, url) VALUES (?,?)", valor)
        db.commit()
        await msg.reply_text("✅ Botão criado. Ele entra no fim de toda lista.")
        await tela_links(update, context)
    elif esperando == "cfg:midia":
        if msg.photo:
            fid, tipo = msg.photo[-1].file_id, "photo"
        elif msg.video:
            fid, tipo = msg.video.file_id, "video"
        elif msg.animation:
            fid, tipo = msg.animation.file_id, "animation"
        else:
            await msg.reply_text("Mande uma foto, vídeo ou GIF, ou toque em Cancelar.")
            return
        context.user_data.pop("esperando")
        set_cfg("midia", fid)
        set_cfg("midia_tipo", tipo)
        await msg.reply_text("✅ Mídia da lista salva.")
        await painel(update, context)
    elif esperando.startswith("cfg:"):
        chave = esperando[4:]
        if not texto:
            await msg.reply_text("Mande em texto, ou toque em Cancelar.")
            return
        valor, erro = validar_config(chave, texto)
        if erro:
            await msg.reply_text(f"❌ {erro} Tente de novo:")
            return
        context.user_data.pop("esperando")
        set_cfg(chave, valor)
        if chave == "horarios":
            agendar(context.application)
        await msg.reply_text(f"✅ {CAMPOS[chave][0]} salvo.")
        await painel(update, context)
    else:
        await menu(update, context)


# ================================================================ comandos


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("/start de %s (%s)", update.effective_user.first_name, update.effective_user.id)
    await menu(update, context)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if eh_admin(update):
        await painel(update, context)


async def cmd_publicar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if eh_admin(update):
        await update.message.reply_text("⏳ Publicando a lista...")
        await update.message.reply_text(await fazer_envio(context.bot), parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Use: /add @seucanal")
        return
    user = update.effective_user
    try:
        chat = await context.bot.get_chat(context.args[0])
        membro = await context.bot.get_chat_member(chat.id, user.id)
    except TelegramError as e:
        await update.message.reply_text(f"❌ Não achei: {e.message}")
        return
    if membro.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER) and not eh_admin(update):
        await update.message.reply_text("❌ Só um administrador do canal pode inscrevê-lo.")
        return
    ok, msg = await registrar(context.bot, chat.id, user.id)
    await pos_registro(context.bot, chat.id, user.id, ok, msg)


async def cmd_buscar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        await resultado_busca(update, " ".join(context.args)[:50])
    else:
        await pedir_busca(update, context)


# ================================================================ modo inline


# ---------------------------------------------------------------- busca global
# Usa uma conta comum do Telegram (Telethon) só para pesquisar canais e grupos
# públicos, como a busca global do app. Liga sozinha quando existe busca.session.
cliente_busca = None
cache_busca: dict[str, tuple[datetime, list]] = {}


async def ligar_busca() -> None:
    global cliente_busca
    if not (API_ID and API_HASH and SESSAO_BUSCA.with_suffix(".session").exists()):
        log.info("Busca global desligada (rode login_busca.py para ligar).")
        return
    try:
        from telethon import TelegramClient
        c = TelegramClient(str(SESSAO_BUSCA), API_ID, API_HASH)
        await c.connect()
        if await c.is_user_authorized():
            cliente_busca = c
            log.info("Busca global ligada.")
        else:
            log.warning("Sessão da busca expirou. Rode login_busca.py de novo.")
            await c.disconnect()
    except Exception as e:  # nunca derruba o bot
        log.warning("Busca global não ligou: %s", e)


def username_do_link(link: str | None) -> str | None:
    if link and link.startswith("https://t.me/") and "+" not in link and "joinchat" not in link:
        return link.rsplit("/", 1)[-1] or None
    return None


def foto(username: str | None) -> str | None:
    return f"https://t.me/i/userpic/320/{username}.jpg" if username else None


async def buscar_global(termo: str) -> list[dict]:
    """Canais e grupos públicos do Telegram inteiro. Guarda 30 min por termo."""
    if not cliente_busca or len(termo) < 3:
        return []
    chave = termo.lower()
    if chave in cache_busca and agora() - cache_busca[chave][0] < timedelta(minutes=30):
        return cache_busca[chave][1]
    from telethon import functions
    from telethon.errors import FloodWaitError
    try:
        res = await cliente_busca(functions.contacts.SearchRequest(q=termo, limit=40))
    except FloodWaitError as e:
        log.warning("Busca global em pausa por %ss (limite do Telegram).", e.seconds)
        return []
    except Exception as e:
        log.warning("Busca global falhou: %s", e)
        return []
    achados = []
    for ch in res.chats:
        if not getattr(ch, "username", None) or not getattr(ch, "title", None):
            continue
        achados.append({"id": ch.id, "titulo": ch.title, "username": ch.username,
                        "membros": getattr(ch, "participants_count", None),
                        "canal": bool(getattr(ch, "broadcast", False))})
    if len(cache_busca) > 500:
        cache_busca.clear()
    cache_busca[chave] = (agora(), achados)
    return achados


def resultado_inline(rid: str, titulo: str, username: str | None, link: str,
                     membros, canal_: bool, extra: str = "") -> InlineQueryResultArticle:
    quem = "inscritos" if canal_ else "membros"
    qtd = f"[{num(membros)} {quem}]" if membros else ""
    desc = " ".join(x for x in (f"@{username}" if username else "", qtd, extra) if x)
    return InlineQueryResultArticle(
        id=rid,
        title=titulo,
        description=desc,
        thumbnail_url=foto(username),
        input_message_content=InputTextMessageContent(
            f"{'📢' if canal_ else '👥'} <b>{esc(titulo)}</b>\n{esc(desc)}\n{link}",
            parse_mode=ParseMode.HTML,
        ),
        reply_markup=kb([U("➡️ Entrar", link)]),
    )


async def inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    termo = update.inline_query.query.strip()[:64]
    rows = db.execute(
        f"SELECT * FROM canais WHERE {NA_LISTA} AND (titulo LIKE ? OR categoria LIKE ?) "
        "ORDER BY membros DESC LIMIT 20",
        (f"%{termo}%", f"%{termo}%"),
    ).fetchall()
    resultados, vistos = [], set()
    for r in rows:  # os da lista vêm primeiro, com ⭐
        u = username_do_link(r["link"])
        vistos.add((u or "").lower())
        resultados.append(resultado_inline(f"l{r['chat_id']}", r["titulo"], u, r["link"],
                                           r["membros"], r["tipo"] == ChatType.CHANNEL, "⭐"))
    for g in await buscar_global(termo):
        if g["username"].lower() in vistos or len(resultados) >= 50:
            continue
        vistos.add(g["username"].lower())
        resultados.append(resultado_inline(f"g{g['id']}", g["titulo"], g["username"],
                                           f"https://t.me/{g['username']}", g["membros"], g["canal"]))
    rotulo = f'Canais e grupos: "{termo}"' if termo else "Canais da lista"
    await update.inline_query.answer(resultados[:50], cache_time=300,
                                     button=InlineQueryResultsButton(rotulo[:64], start_parameter="lista"))


# ================================================================ montagem e envio da lista


def montar_teclado(bot, escolhidos) -> InlineKeyboardMarkup:
    """Lista em 1, 2 ou 3 colunas + links avulsos + botões fixos."""
    col = int(cfg("colunas"))
    corte = {1: 60, 2: 28, 3: 18}[col]
    links = db.execute("SELECT * FROM links ORDER BY id").fetchall()
    # links avulsos entram na mesma grade, no mesmo número de colunas
    bts = ([U(l["texto"][:corte], l["url"]) for l in links if l["posicao"] == "topo"]
           + [U(c["titulo"][:corte], c["link"]) for c in escolhidos]
           + [U(l["texto"][:corte], l["url"]) for l in links if l["posicao"] != "topo"])
    grade = [bts[i:i + col] for i in range(0, len(bts), col)]
    rodape = [U("➕ Adicione seu canal", f"https://t.me/{bot.username}?start=lista")]
    if ligado("ver_mais"):
        rodape.append(U("📂 Ver mais", WEB_URL))
    pintar(grade, rodape)
    return InlineKeyboardMarkup(grade + [rodape])


def pintar(grade: list, rodape: list) -> None:
    """Cor dos botões (Telegram aceita azul, verde e vermelho). Sorteio muda a cada envio."""
    modo = cfg("cores")
    if modo == "nenhuma":
        return
    tres = ["primary", "success", "danger"]
    if modo == "sorteio":
        modo = random.choice(["azul", "verde", "vermelho", "xadrez", "faixas", "aleatorio"])
    for i, linha in enumerate(grade):
        for j, b in enumerate(linha):
            cor = {"azul": "primary", "verde": "success", "vermelho": "danger",
                   "xadrez": tres[(i + j) % 2],
                   "faixas": tres[i % 3],
                   "aleatorio": random.choice(tres)}[modo]
            linha[j] = InlineKeyboardButton(b.text, url=b.url, style=cor)
    rodape[0] = InlineKeyboardButton(rodape[0].text, url=rodape[0].url, style="success")
    if len(rodape) > 1:
        rodape[1] = InlineKeyboardButton(rodape[1].text, url=rodape[1].url, style="primary")


async def mandar_lista(bot, chat_id: int, teclado):
    midia, tipo, texto = cfg("midia"), cfg("midia_tipo"), cfg("descricao")
    extra = {"reply_markup": teclado, "protect_content": ligado("proteger")}
    if midia and tipo == "video":
        return await bot.send_video(chat_id, midia, caption=texto, **extra)
    if midia and tipo == "animation":
        return await bot.send_animation(chat_id, midia, caption=texto, **extra)
    if midia:
        return await bot.send_photo(chat_id, midia, caption=texto, **extra)
    return await bot.send_message(chat_id, texto, disable_web_page_preview=True, **extra)


async def previa(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = db.execute(f"SELECT * FROM canais WHERE {NA_LISTA} ORDER BY membros DESC LIMIT ?",
                      (int(cfg("maximo")),)).fetchall()
    chat = update.effective_chat
    await mandar_lista(context.bot, chat.id, montar_teclado(context.bot, rows))
    await chat.send_message(f"👆 Assim a lista aparece ({len(rows)} canais, {cfg('colunas')} coluna(s)).",
                            reply_markup=kb([B("⬅️ Painel", "adm")]))


async def apagar_envios(bot, tudo: bool = False) -> int:
    if tudo:
        rows = db.execute("SELECT rowid, * FROM envios").fetchall()
    else:
        rows = db.execute("SELECT rowid, * FROM envios WHERE apagar_em <= ?",
                          (agora().isoformat(),)).fetchall()
    for r in rows:
        db.execute("DELETE FROM envios WHERE rowid=?", (r["rowid"],))
        db.commit()
        try:
            await bot.delete_message(r["chat_id"], r["message_id"])
        except TelegramError:
            pass  # já apagada ou sem permissão
    return len(rows)


AVISO_SAIDA = ("⚠️ <b>{titulo}</b> foi removido da lista de divulgação.\n\n"
               "Motivo: {motivo}\n\n"
               "A partir de agora o link do seu canal deixa de aparecer nas listas e no site"
               "{convite}. Para voltar, fale com o suporte.")


async def tirar_da_lista(bot, chat_id: int, motivo: str, motivo_admin: str) -> None:
    """Remove o canal, desativa o link de convite criado pelo bot e avisa o dono e você."""
    r = canal(chat_id)
    if not r or not r["ativo"]:
        return
    db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (chat_id,))
    db.execute("DELETE FROM envios WHERE chat_id=?", (chat_id,))
    db.commit()
    convite = ""
    if r["link"] and "/+" in r["link"]:  # link de convite (canal privado), não o @ público
        try:
            await bot.revoke_chat_invite_link(chat_id, r["link"])
            convite = ", e o link de convite que eu criei foi desativado"
        except TelegramError:
            pass  # o link não foi criado pelo bot ou o bot já saiu
    sup = url_suporte()
    await avisar(bot, r["dono_id"],
                 AVISO_SAIDA.format(titulo=esc(r["titulo"]), motivo=motivo, convite=convite),
                 kb([U("📞 Suporte", sup)]) if sup else None)
    await avisar_admin(bot, f"🚫 {icone(r)} <b>{esc(r['titulo'])}</b> saiu da lista: {motivo_admin}",
                       kb([B("↩️ Ver canal", f"cadm:{chat_id}")]))
    log.info("Saiu da lista (%s): %s", motivo_admin, r["titulo"])


async def job_vigia(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confere se a lista ainda está no canal. Bot não recebe aviso de mensagem apagada,
    então ele "reedita" os mesmos botões: se a mensagem sumiu, o Telegram responde
    "message to edit not found"."""
    bot = context.bot
    limite = (agora() + timedelta(minutes=5)).isoformat()  # perto da hora, quem apaga é o bot
    rows = db.execute("SELECT rowid, * FROM envios WHERE teclado IS NOT NULL AND apagar_em > ?",
                      (limite,)).fetchall()
    for r in rows:
        try:
            await bot.edit_message_reply_markup(
                r["chat_id"], r["message_id"],
                reply_markup=InlineKeyboardMarkup.de_json(json.loads(r["teclado"]), bot))
        except BadRequest as e:
            erro = str(e).lower()
            if "not found" not in erro:
                continue  # "not modified" = a lista está lá
            if not db.execute("SELECT 1 FROM envios WHERE rowid=?", (r["rowid"],)).fetchone():
                continue  # o próprio bot apagou neste meio-tempo
            await tirar_da_lista(bot, r["chat_id"],
                                 "a lista foi apagada do canal antes da hora, sem autorização.",
                                 "apagou a lista antes da hora.")
        except TelegramError:
            continue  # sem acesso: a saída do bot é tratada em mudou_status
        await asyncio.sleep(0.2)


async def job_limpeza(context: ContextTypes.DEFAULT_TYPE) -> None:
    await apagar_envios(context.bot)


async def fazer_envio(bot) -> str:
    """Publica a lista nos canais/grupos escolhidos. Devolve o resumo."""
    await apagar_envios(bot, tudo=True)  # a lista anterior sai antes da nova entrar

    minimo, maximo = int(cfg("minimo")), int(cfg("maximo"))
    # rodízio: quem ficou mais tempo sem aparecer vem primeiro
    candidatos = db.execute(
        f"SELECT * FROM canais WHERE {NA_LISTA} "
        "ORDER BY ultimo_envio IS NOT NULL, ultimo_envio, membros DESC"
    ).fetchall()

    escolhidos = []
    for c in candidatos:
        if len(escolhidos) >= maximo:
            break
        if not aceita_tipo(c["tipo"]):
            continue
        try:
            membros = await bot.get_chat_member_count(c["chat_id"])
            eu = await bot.get_chat_member(c["chat_id"], bot.id)
        except TelegramError:
            db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (c["chat_id"],))
            continue
        if eu.status != ChatMemberStatus.ADMINISTRATOR:
            db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (c["chat_id"],))
            await avisar(bot, c["dono_id"],
                         f"⚠️ <b>{esc(c['titulo'])}</b> saiu da lista: não sou mais administrador.")
            continue
        db.execute("UPDATE canais SET membros=? WHERE chat_id=?", (membros, c["chat_id"]))
        if membros < minimo:
            continue
        escolhidos.append(dict(c, membros=membros))
    db.commit()

    if len(escolhidos) < 2:
        return f"⚠️ Lista não publicada: só {len(escolhidos)} apto (precisa de pelo menos 2)."

    ordem = cfg("ordem")
    exibir = list(escolhidos)
    if ordem == "membros":
        exibir.sort(key=lambda c: c["membros"], reverse=True)
    elif ordem == "aleatoria":
        random.shuffle(exibir)
    teclado = montar_teclado(bot, exibir)

    apagar_em = (agora() + timedelta(hours=int(cfg("horas_apagar")))).isoformat()
    ok, falhas = 0, []
    for c in escolhidos:
        try:
            msg = await mandar_lista(bot, c["chat_id"], teclado)
            db.execute("INSERT INTO envios VALUES (?,?,?,?)", (c["chat_id"], msg.message_id, apagar_em,
                                                                  json.dumps(teclado.to_dict())))
            db.execute("UPDATE canais SET ultimo_envio=? WHERE chat_id=?",
                       (agora().isoformat(), c["chat_id"]))
            ok += 1
            if ligado("fixar"):
                try:
                    await bot.pin_chat_message(c["chat_id"], msg.message_id, disable_notification=True)
                except TelegramError:
                    pass  # sem permissão de fixar
        except Forbidden:
            db.execute("UPDATE canais SET ativo=0 WHERE chat_id=?", (c["chat_id"],))
            falhas.append(c["titulo"])
        except TelegramError as e:
            log.warning("Falha em %s: %s", c["titulo"], e)
            falhas.append(c["titulo"])
        db.commit()
        await asyncio.sleep(0.1)  # respeita o limite do Telegram

    resumo = f"✅ Lista publicada em <b>{ok}</b> canais/grupos."
    if falhas:
        resumo += "\n❌ Falhou em: " + esc(", ".join(falhas))
    return resumo


async def job_lista(context: ContextTypes.DEFAULT_TYPE) -> None:
    resumo = await fazer_envio(context.bot)
    await avisar_admin(context.bot, resumo)


def agendar(app: Application | None) -> None:
    if app is None:
        return
    for job in app.job_queue.get_jobs_by_name("lista"):
        job.schedule_removal()
    dias = tuple(sorted(int(d) for d in cfg("dias")))
    for hm in cfg("horarios").split(","):
        h, m = map(int, hm.strip().split(":"))
        app.job_queue.run_daily(job_lista, time=dtime(h, m, tzinfo=TZ), days=dias, name="lista")
    log.info("Lista agendada: %s (%s)", cfg("horarios"), texto_dias())


# ================================================================ versão web


def dados_site(bot, fotos: dict | None = None) -> dict:
    """Só campos públicos: nada de dono, chat_id ou configuração interna."""
    rows = db.execute(f"SELECT * FROM canais WHERE {NA_LISTA} ORDER BY membros DESC").fetchall()
    canais = []
    for r in rows:
        canais.append({
            "titulo": r["titulo"],
            "link": r["link"],
            "username": username_do_link(r["link"]),
            "membros": r["membros"] or 0,
            "categoria": r["categoria"] or "📦 Outros",
            "tipo": "grupo" if r["tipo"] in ("group", "supergroup") else "canal",
            "desde": r["criado_em"],
            "foto": (fotos or {}).get(r["chat_id"]),
        })
    base = f"https://t.me/{bot.username}"
    return {
        "bot": {"username": bot.username, "nome": bot.first_name,
                "adicionar_canal": (f"{base}?startchannel&admin=post_messages+delete_messages"
                                    "+edit_messages+invite_users"),
                "adicionar_grupo": f"{base}?startgroup&admin=delete_messages+pin_messages+invite_users",
                "aceita": cfg("aceita")},
        "horarios": cfg("horarios"),
        "categorias": CATEGORIAS,
        "canais": canais,
    }


async def git(*args) -> int:
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "user.name=bot-lista", "-c", "user.email=bot-lista@users.noreply.github.com",
        *args, cwd=PASTA, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode:
        log.warning("git %s: %s", args[0], err.decode(errors="ignore")[-300:])
    return proc.returncode


async def baixar_fotos(bot) -> dict:
    """Foto de cada canal pela API do bot, salva em docs/fotos/<id único>.jpg.
    O nome muda quando o dono troca a foto; as que ninguém usa mais são apagadas."""
    pasta = ARQ_SITE.parent / "fotos"
    pasta.mkdir(parents=True, exist_ok=True)
    fotos = {}
    for r in db.execute(f"SELECT chat_id FROM canais WHERE {NA_LISTA}").fetchall():
        try:
            chat = await bot.get_chat(r["chat_id"])
            if not chat.photo:
                continue
            nome = f"{chat.photo.small_file_unique_id}.jpg"
            if not (pasta / nome).exists():
                arq = await bot.get_file(chat.photo.small_file_id)
                await arq.download_to_drive(pasta / nome)
            fotos[r["chat_id"]] = f"fotos/{nome}"
        except TelegramError as e:
            log.info("Foto de %s: %s", r["chat_id"], e)
    usadas = {Path(f).name for f in fotos.values()}
    for f in pasta.glob("*.jpg"):
        if f.name not in usadas:
            f.unlink()
    return fotos


async def job_site(context: ContextTypes.DEFAULT_TYPE) -> None:
    novo = dados_site(context.bot, await baixar_fotos(context.bot))
    try:
        velho = json.loads(ARQ_SITE.read_text(encoding="utf-8"))
        velho.pop("atualizado", None)
    except (OSError, ValueError):
        velho = None
    if velho == novo:
        return
    novo["atualizado"] = agora().isoformat(timespec="minutes")
    ARQ_SITE.parent.mkdir(exist_ok=True)
    ARQ_SITE.write_text(json.dumps(novo, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Site: canais.json atualizado (%s canais).", len(novo["canais"]))
    if os.environ.get("GITHUB_ACTIONS") == "true":  # na nuvem, publica no GitHub Pages
        await git("add", "-A", "docs")
        if await git("commit", "-m", "site: lista atualizada") == 0:
            await git("pull", "--rebase", "-X", "theirs")
            await git("push")


async def ao_iniciar(app: Application) -> None:
    agendar(app)
    app.job_queue.run_repeating(job_site, interval=1800, first=15)
    app.job_queue.run_repeating(job_vigia, interval=600, first=60)
    try:
        await app.bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp("Lista", WebAppInfo(WEB_URL)))
    except TelegramError as e:
        log.warning("Botão do Mini App: %s", e)
    app.job_queue.run_repeating(job_limpeza, interval=300, first=10)
    await ligar_busca()
    # No GitHub Actions cada rodada dura no máximo 6 h: sai antes, a próxima assume.
    limite = int(os.environ.get("LIMITE_MIN", "0") or 0)
    if limite:
        app.job_queue.run_once(parar, limite * 60)
        log.info("Rodada limitada a %s min.", limite)


async def parar(context: ContextTypes.DEFAULT_TYPE) -> None:
    log.info("Fim da rodada; salvando e saindo.")
    context.application.stop_running()


async def erro(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.warning("Erro: %s", context.error)


# ================================================================ main


def main() -> None:
    if not TOKEN or not ADMIN_ID:
        raise SystemExit("Preencha BOT_TOKEN e ADMIN_ID no arquivo .env (veja .env.exemplo).")

    app = Application.builder().token(TOKEN).post_init(ao_iniciar).concurrent_updates(True).build()
    privado = filters.ChatType.PRIVATE

    app.add_handler(TypeHandler(Update, registrar_usuario), group=-1)
    app.add_handler(ChatMemberHandler(mudou_status, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", cmd_start, privado))
    app.add_handler(CommandHandler("admin", cmd_admin, privado))
    app.add_handler(CommandHandler("publicar", cmd_publicar, privado))
    app.add_handler(CommandHandler("add", cmd_add, privado))
    app.add_handler(CommandHandler("buscar", cmd_buscar, privado))
    app.add_handler(CallbackQueryHandler(botoes))
    app.add_handler(InlineQueryHandler(inline))
    app.add_handler(MessageHandler(privado & ~filters.COMMAND, mensagem_privada))
    app.add_error_handler(erro)

    log.info("Bot rodando. Ctrl+C para parar.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
