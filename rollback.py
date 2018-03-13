import sqlite3, os

def rollback(height):

    try:
        os.remove("static/index.db")
    except:
        pass

    conn = sqlite3.connect('static/ledger.db')
    conn.text_factory = str
    c = conn.cursor()

    hyp = sqlite3.connect('static/hyper.db')
    hyp.text_factory = str
    h = conn.cursor()

    c.execute("DELETE FROM transactions WHERE block_height > ?", (height,))
    conn.commit()
    c.execute("DELETE FROM misc WHERE block_height > ?", (height,))
    conn.commit()

    h.execute("DELETE FROM transactions WHERE block_height > ?", (height,))
    hyp.commit()
    h.execute("DELETE FROM misc WHERE block_height > ?", (height,))
    hyp.commit()

if __name__ == "__main__":
    rollback("553500")

