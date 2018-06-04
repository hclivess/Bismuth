import re

def replace_regex(string, replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string

def ann_get(cursor, ann_addr):
    try:
        cursor.execute("SELECT openfield FROM transactions WHERE address = ? AND openfield LIKE ? ORDER BY block_height DESC LIMIT 1", (ann_addr, "ann=%"))
        result = cursor.fetchone()[0]
        ann_stripped = replace_regex(result, "ann=")
    except:
        ann_stripped = None
    return ann_stripped

def ann_ver_get(cursor, ann_addr):
    try:
        cursor.execute("SELECT openfield FROM transactions WHERE address = ? AND openfield LIKE ? ORDER BY block_height DESC LIMIT 1", (ann_addr, "annver=%"))
        result = cursor.fetchone()[0]
        ann_ver_stripped = replace_regex(result, "annver=")
    except:
        ann_ver_stripped = None
    return ann_ver_stripped
