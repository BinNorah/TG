"""Conexiones SQLite, migraciones y estado persistente de Flamie."""
import sqlite3

from flamie.core.config import DB_PATH

BUSY_TIMEOUT_MS = 15_000


def get_db():
    con = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return con


def _column_exists(con, table, column):
    return any(row[1] == column for row in con.execute(f"PRAGMA table_info({table})"))


def init_db():
    """Crea el esquema conservando las tablas existentes y aplica migraciones."""
    con = get_db()
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (singleton INTEGER PRIMARY KEY CHECK(singleton=1), version INTEGER NOT NULL)"
        )
        row = con.execute("SELECT version FROM schema_version WHERE singleton=1").fetchone()
        version = row[0] if row else 0

        # La base histórica no tenía tabla de versión; se adopta como versión 1.
        if version < 1:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS mensajes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, nombre TEXT,
                    mensaje TEXT, fecha TEXT, hora TEXT DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS usuarios_notificados (user_id INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS chat_users (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
                    first_name TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_chat_users_username ON chat_users(chat_id, username);
                CREATE TABLE IF NOT EXISTS custom_commands (
                    command TEXT PRIMARY KEY, response TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS chat_settings (
                    chat_id INTEGER NOT NULL, setting TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY (chat_id, setting)
                );
                CREATE TABLE IF NOT EXISTS chat_rules (
                    chat_id INTEGER PRIMARY KEY, rules TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS group_activity (
                    chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, username TEXT,
                    first_name TEXT, count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                );
            """)
            if not _column_exists(con, "mensajes", "hora"):
                con.execute("ALTER TABLE mensajes ADD COLUMN hora TEXT DEFAULT ''")
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,1) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 1

        if version < 2:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS activation_states (
                    user_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL CHECK(state IN ('plataforma', 'codigo')),
                    platform TEXT,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS userbot_usuarios_respondidos (
                    user_id INTEGER PRIMARY KEY,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
            """)
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,2) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 2

        if version < 3:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS mod_actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    actor_id INTEGER NOT NULL,
                    target_user_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    duration_seconds INTEGER,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_mod_actions_chat_created
                    ON mod_actions(chat_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_mod_actions_target_created
                    ON mod_actions(chat_id, target_user_id, created_at DESC);
            """)
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,3) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 3

        if version < 4:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS captcha_pending (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    deadline INTEGER NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id,user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_captcha_deadline ON captcha_pending(deadline);
                CREATE TABLE IF NOT EXISTS chat_filters (
                    chat_id INTEGER NOT NULL,
                    trigger TEXT NOT NULL,
                    response TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id,trigger)
                );
                CREATE TABLE IF NOT EXISTS chat_notes (
                    chat_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id,name)
                );
                CREATE TABLE IF NOT EXISTS blacklist_words (
                    chat_id INTEGER NOT NULL,
                    word TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id,word)
                );
            """)
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,4) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 4

        if version < 5:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    due_at INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','sent','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status,due_at);
                CREATE TABLE IF NOT EXISTS chat_afk (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(chat_id,user_id)
                );
            """)
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,5) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 5

        if version < 6:
            if not _column_exists(con, "group_activity", "xp"):
                con.execute("ALTER TABLE group_activity ADD COLUMN xp INTEGER NOT NULL DEFAULT 0")
            if not _column_exists(con, "group_activity", "last_xp"):
                con.execute("ALTER TABLE group_activity ADD COLUMN last_xp INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS idx_group_activity_xp ON group_activity(chat_id,xp DESC)")
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,6) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
            version = 6

        if version < 7:
            con.execute("""CREATE TABLE IF NOT EXISTS chat_registry (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL DEFAULT 'group',
                last_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")
            con.execute("CREATE TABLE IF NOT EXISTS global_bans (
                user_id INTEGER PRIMARY KEY,
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )")
            con.execute(
                "INSERT INTO schema_version(singleton,version) VALUES (1,7) "
                "ON CONFLICT(singleton) DO UPDATE SET version=excluded.version"
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def load_activation_states():
    con = get_db()
    try:
        rows = con.execute("SELECT user_id, state, platform FROM activation_states").fetchall()
        waiting_platform = {row[0] for row in rows if row[1] == "plataforma"}
        waiting_code = {row[0]: row[2] for row in rows if row[1] == "codigo"}
        return waiting_platform, waiting_code
    finally:
        con.close()


def save_activation_state(user_id, state, platform=None):
    con = get_db()
    try:
        con.execute(
            "INSERT INTO activation_states(user_id,state,platform,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT(user_id) DO UPDATE SET state=excluded.state, platform=excluded.platform, "
            "updated_at=CURRENT_TIMESTAMP",
            (user_id, state, platform),
        )
        con.commit()
    finally:
        con.close()


def clear_activation_state(user_id):
    con = get_db()
    try:
        con.execute("DELETE FROM activation_states WHERE user_id=?", (user_id,))
        con.commit()
    finally:
        con.close()


def load_userbot_respondidos():
    con = get_db()
    try:
        return {row[0] for row in con.execute("SELECT user_id FROM userbot_usuarios_respondidos")}
    finally:
        con.close()


def mark_userbot_respondido(user_id):
    con = get_db()
    try:
        con.execute(
            "INSERT OR IGNORE INTO userbot_usuarios_respondidos(user_id) VALUES(?)",
            (user_id,),
        )
        con.commit()
    finally:
        con.close()


def query(sql, params=()):
    con = get_db()
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def load_usuarios_notificados():
    return {row[0] for row in query("SELECT user_id FROM usuarios_notificados")}


def record_mod_action(chat_id, actor_id, target_user_id, action, reason="", duration_seconds=None):
    con = get_db()
    try:
        con.execute(
            "INSERT INTO mod_actions(chat_id,actor_id,target_user_id,action,reason,duration_seconds) "
            "VALUES(?,?,?,?,?,?)",
            (chat_id, actor_id, target_user_id, action, reason, duration_seconds),
        )
        con.commit()
    finally:
        con.close()
