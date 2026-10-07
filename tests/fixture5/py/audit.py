def record(conn, item):
    conn.execute("INSERT INTO audit (item) VALUES (?)", (item,))


def recent(conn):
    return conn.execute("SELECT item FROM audit ORDER BY id DESC").fetchall()


def describe(conn):
    return "Select a file from disk"
