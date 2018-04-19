# icons created using http://www.winterdrache.de/freeware/png2ico/
import sqlite3
import PIL.Image, PIL.ImageTk, pyqrcode, os, hashlib, time, base64, connections, icons, log, socks, ast, options, tarfile, glob, essentials, re, platform
from tokens import *
from decimal import *
from bisurl import *
from quantizer import quantize_eight
import csv

import matplotlib
matplotlib.use('TkAgg')
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2TkAgg
from matplotlib.figure import Figure

# import keys

# globalize
global block_height_old
global statusget
global key
global private_key_readable
global encrypted
global unlocked
global public_key_hashed
global myaddress
global private_key_load
global public_key_load
global s


# data for charts
stats_nodes_count_list = []
stats_thread_count_list = []
stats_consensus_list = []
stats_consensus_percentage_list = []
stats_diff_list_0 = []
stats_diff_list_1 = []
stats_diff_list_2 = []
stats_diff_list_3 = []
stats_diff_list_4 = []
stats_diff_list_5 = []
stats_diff_list_6 = []
# data for charts

if os.path.exists("privkey.der"):
    private_key_load = "privkey.der"
else:
    private_key_load = "privkey_encrypted.der"

public_key_load = "pubkey.der"

key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, myaddress = essentials.keys_load(private_key_load, public_key_load)

print(getcontext())

config = options.Get()
config.read()
debug_level = config.debug_level_conf
full_ledger = config.full_ledger_conf
port = config.port
light_ip = config.light_ip_conf
version = config.version_conf
terminal_output = config.terminal_output
gui_scaling = config.gui_scaling

if "testnet" in version:
    port = 2829
    light_ip = "127.0.0.1"

from datetime import datetime
from Crypto.PublicKey import RSA
from Crypto.Signature import PKCS1_v1_5
from Crypto.Hash import SHA
from Crypto.Random import get_random_bytes
from Crypto.Cipher import AES, PKCS1_OAEP

from simplecrypt import encrypt, decrypt
from tkinter import filedialog, messagebox
from tkinter import *

# app_log = log.log("gui.log", debug_level)
app_log = log.log("gui.log", debug_level, terminal_output)

essentials.keys_check(app_log)
essentials.db_check(app_log)



def address_validate(address):
    if re.match ('[abcdef0123456789]{56}', address):
        return True
    else:
        return False

def create_url_clicked(app_log, command, recipient, amount, openfield):
    """isolated function so no GUI leftovers are in bisurl.py"""
    result = create_url(app_log, command, recipient, amount, openfield)
    url.delete(0, END)
    url.insert(0, result)


def read_url_clicked(app_log, url):
    """isolated function so no GUI leftovers are in bisurl.py"""
    result = (read_url(app_log, url))

    recipient.delete(0, END)
    amount.delete(0, END)
    openfield.delete("1.0", END)

    recipient.insert(0, result[1])  # amount
    amount.insert(0, result[2])  # recipient
    openfield.insert(INSERT, result[3])  # openfield


def node_connect():
    while True:
        try:
            global s
            s = socks.socksocket ()
            s.settimeout (3)

            s.connect((light_ip, int(port)))
            app_log.warning("Status: Wallet connected")
            break
        except Exception as e:
            app_log.warning("Status: Wallet cannot connect to the node ({}), perhaps it is still starting up, retrying".format(e))
            time.sleep(1)


def replace_regex(string, replace):
    replaced_string = re.sub(r'^{}'.format(replace), "", string)
    return replaced_string


def alias_register(alias_desired):
    connections.send(s, "aliascheck", 10)
    connections.send(s, alias_desired, 10)

    result = connections.receive(s, 10)

    if result == "Alias free":
        send("0", myaddress, "alias=" + alias_desired)
        pass
    else:
        top9 = Toplevel()
        top9.title("Name already registered")

        registered_label = Label(top9, text="Name already registered")
        registered_label.grid(row=0, column=0, sticky=N + W, padx=15, pady=(5, 0))
        dismiss = Button(top9, text="Dismiss", command=top9.destroy)
        dismiss.grid(row=3, column=0, sticky=W + E, padx=15, pady=(5, 5))


def help():
    top13 = Toplevel()
    top13.title("Help")
    aliases_box = Text(top13, width=100)
    aliases_box.grid(row=0, pady=0)

    aliases_box.insert(INSERT, "Encrypt with PK:\n Encrypt the data with the recipient's private key. Only they will be able to view it.")
    aliases_box.insert(INSERT, "\n\n")
    aliases_box.insert(INSERT, "Mark as Message:\n Mark data as message. The recipient will be able to view it in the message section.")
    aliases_box.insert(INSERT, "\n\n")
    aliases_box.insert(INSERT, "Base64 Encoding:\n Encode the data with base64, it is a group of binary-to-text encoding scheme that representd binary data in an ASCII string format by translating it into a radix-64 representation.")
    aliases_box.insert(INSERT, "\n\n")
    aliases_box.insert(INSERT, "Permanent:\n Operation entry in the blockchain.")
    aliases_box.insert(INSERT, "\n\n")
    aliases_box.insert(INSERT, "Alias Recipient:\n Use an alias of the recipient in the recipient field if they have one registered")
    aliases_box.insert(INSERT, "\n\n")
    aliases_box.insert(INSERT, "Resolve Aliases:\n Show aliases instead of addressess where applicable in the table below.")
    aliases_box.insert(INSERT, "\n\n")

    close = Button(top13, text="Close", command=top13.destroy)
    close.grid(row=3, column=0, sticky=W + E, padx=15, pady=(5, 5))


def data_insert_clear():
    openfield.delete('1.0', END)  # remove previous


def all_spend_clear():
    all_spend_var.set(False)

    amount.delete(0, END)
    amount.insert(0, 0)


def all_spend():
    # all_spend_var.set(True)
    all_spend_check()


def all_spend_check():
    if all_spend_var.get() == True:
        openfield_fee_calc = openfield.get("1.0", END).strip()

        if encode_var.get() == True and msg_var.get() == False:
            openfield_fee_calc = base64.b64encode(openfield_fee_calc.encode("utf-8")).decode("utf-8")

        if msg_var.get() == True and encode_var.get() == True:
            openfield_fee_calc = "bmsg=" + base64.b64encode(openfield_fee_calc.encode("utf-8")).decode("utf-8")
        if msg_var.get() == True and encode_var.get() == False:
            openfield_fee_calc = "msg=" + openfield_fee_calc
        if encrypt_var.get() == True:
            openfield_fee_calc = "enc=" + str(openfield_fee_calc)

        fee_from_all = fee_calculate(openfield_fee_calc)
        amount.delete(0, END)
        amount.insert(0, (Decimal(balance_raw.get()) - Decimal(fee_from_all)))


def fee_calculate(openfield):
    fee = Decimal("0.01") + (Decimal(len(openfield)) / Decimal("100000"))  # 0.01 dust
    if "token:issue:" in openfield:
        fee = Decimal(fee) + Decimal("10")
    if "alias=" in openfield:
        fee = Decimal(fee) + Decimal("1")
    fee = quantize_eight(fee)

    return fee


def fingerprint():
    root.filename = filedialog.askopenfilename (multiple=True, initialdir="", title="Select files for fingerprinting", filetypes=[("Files", "*")])

    dict = {}

    for file in root.filename:
        with open(file, 'rb') as fp:
            data = hashlib.blake2b(fp.read()).hexdigest()
            dict[os.path.split(file)[-1]] = data

    openfield.insert (INSERT, dict)



def keys_load_dialog():
    global key
    global key
    global private_key_readable
    global encrypted
    global unlocked
    global public_key_hashed
    global myaddress
    global private_key_load
    global public_key_load

    root.filename = filedialog.askopenfilename(multiple=True, initialdir="", title="Select wallet files", filetypes=[("Wallet keys", "*key*.der")])
    for file in root.filename:
        if "priv" in file:
            private_key_load = file
        if "pub" in file:
            public_key_load = file

    key, public_key_readable, private_key_readable, encrypted, unlocked, public_key_hashed, myaddress = essentials.keys_load(private_key_load, public_key_load)

    encryption_button_refresh()

    gui_address.delete(0, END)
    gui_address.insert(INSERT, myaddress)

    refresh(myaddress, s)


def keys_backup():
    root.filename = filedialog.asksaveasfilename(initialdir="", title="Select backup file", filetypes=(("gzip", "*.gz"),))

    if not root.filename == "":
        if not root.filename.endswith(".tar.gz"):
            root.filename = root.filename + ".tar.gz"

        der_files = glob.glob("*.der")

        tar = tarfile.open(root.filename, "w:gz")
        for der_file in der_files:
            tar.add(der_file, arcname=der_file)
        tar.close()


def watch():
    address = gui_address.get()
    refresh(address, s)


def unwatch():
    gui_address.delete(0, END)
    gui_address.insert(INSERT, myaddress)
    refresh(myaddress, s)


def aliases_list():
    top12 = Toplevel()
    top12.title("Your aliases")
    aliases_box = Text(top12, width=100)
    aliases_box.grid(row=0, pady=0)

    connections.send(s, "aliasget", 10)
    connections.send(s, myaddress, 10)

    aliases_self = connections.receive(s, 10)

    for x in aliases_self:
        aliases_box.insert(INSERT, replace_regex(x[0], "alias="))
        aliases_box.insert(INSERT, "\n")

    close = Button(top12, text="Close", command=top12.destroy)
    close.grid(row=3, column=0, sticky=W + E, padx=15, pady=(5, 5))


def recipient_insert():
    recipient.delete(0, END)
    recipient.insert(0, root.clipboard_get())


def address_insert():
    gui_address.delete(0, END)
    gui_address.insert(0, root.clipboard_get())


def data_insert():
    openfield.delete('1.0', END)  # remove previous
    openfield.insert(INSERT, root.clipboard_get())


def url_insert():
    url.delete(0, END)  # remove previous
    url.insert(0, root.clipboard_get())


def address_copy():
    root.clipboard_clear()
    root.clipboard_append(gui_address.get())


def url_copy():
    root.clipboard_clear()
    root.clipboard_append(url.get())


def recipient_copy():
    root.clipboard_clear()
    root.clipboard_append(recipient.get())


def percentage(percent, whole):
    return (Decimal(percent) * Decimal(whole) / 100)


def alias():
    alias_var = StringVar()

    # enter password
    top8 = Toplevel()
    top8.title("Enter Desired Name")

    alias_label = Label(top8, text="Input name")
    alias_label.grid(row=0, column=0, sticky=N + W, padx=15, pady=(5, 0))

    input_alias = Entry(top8, textvariable=alias_var)
    input_alias.grid(row=1, column=0, sticky=N + E, padx=15, pady=(0, 5))

    dismiss = Button(top8, text="Register", command=lambda: alias_register(alias_var.get().strip()))
    dismiss.grid(row=2, column=0, sticky=W + E, padx=15, pady=(15, 0))

    dismiss = Button(top8, text="Dismiss", command=top8.destroy)
    dismiss.grid(row=3, column=0, sticky=W + E, padx=15, pady=(5, 5))


def encrypt_get_password():
    # enter password
    top3 = Toplevel()
    top3.title("Enter Password")

    password_label = Label(top3, text="Input password")
    password_label.grid(row=0, column=0, sticky=N + W, padx=15, pady=(5, 0))

    input_password = Entry(top3, textvariable=password_var_enc, show='*')
    input_password.grid(row=1, column=0, sticky=N + E, padx=15, pady=(0, 5))

    confirm_label = Label(top3, text="Confirm password")
    confirm_label.grid(row=2, column=0, sticky=N + W, padx=15, pady=(5, 0))

    input_password_con = Entry(top3, textvariable=password_var_con, show='*')
    input_password_con.grid(row=3, column=0, sticky=N + E, padx=15, pady=(0, 5))

    enter = Button(top3, text="Encrypt", command=lambda: encrypt_fn(top3))
    enter.grid(row=4, column=0, sticky=W + E, padx=15, pady=(5, 5))

    cancel = Button(top3, text="Cancel", command=top3.destroy)
    cancel.grid(row=5, column=0, sticky=W + E, padx=15, pady=(5, 5))
    # enter password


def lock_fn(button):
    key = None
    decrypt_b.configure(text="Unlock", state=NORMAL)
    lock_b.configure(text="Locked", state=DISABLED)
    sign_b.configure(text="Sign (locked)", state=DISABLED)
    password_var_dec.set("")


def encrypt_fn(destroy_this):
    password = password_var_enc.get()
    password_conf = password_var_con.get()

    if password == password_conf:

        ciphertext = encrypt(password, private_key_readable)

        pem_file = open(private_key_load, 'wb')

        pem_file.write(base64.b64encode(ciphertext))
        pem_file.close()

        # encrypt_b.configure(text="Encrypted", state=DISABLED)
        destroy_this.destroy()
        # lock_b.configure(text="Lock", state=NORMAL)
    else:
        mismatch = Toplevel()
        mismatch.title("Bismuth")

        mismatch_msg = Message(mismatch, text="Password mismatch", width=100)
        mismatch_msg.pack()

        mismatch_button = Button(mismatch, text="Continue", command=mismatch.destroy)
        mismatch_button.pack(padx=15, pady=(5, 5))


def decrypt_get_password():
    # enter password
    top4 = Toplevel()
    top4.title("Enter Password")

    input_password = Entry(top4, textvariable=password_var_dec, show='*')
    input_password.grid(row=0, column=0, sticky=N + E, padx=15, pady=(5, 5))

    enter = Button(top4, text="Unlock", command=lambda: decrypt_fn(top4))
    enter.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))

    cancel = Button(top4, text="Cancel", command=top4.destroy)
    cancel.grid(row=2, column=0, sticky=W + E, padx=15, pady=(5, 5))
    # enter password


def decrypt_fn(destroy_this):
    global key
    try:
        password = password_var_dec.get()
        encrypted_privkey = open(private_key_load, 'rb').read()
        decrypted_privkey = decrypt(password, base64.b64decode(encrypted_privkey))  # decrypt privkey

        key = RSA.importKey(decrypted_privkey)  # be able to sign

        # print key
        decrypt_b.configure(text="Unlocked", state=DISABLED)
        lock_b.configure(text="Lock", state=NORMAL)
        sign_b.configure(text="Sign Message", state=NORMAL)
        destroy_this.destroy()
    except:
        raise
        top6 = Toplevel()
        top6.title("Locked")
        Label(top6, text="Wrong password", width=20).grid(row=0, pady=0)
        cancel = Button(top6, text="Cancel", command=top6.destroy)
        cancel.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))

    return key


def send_confirm(amount_input, recipient_input, openfield_input):
    amount_input = quantize_eight(amount_input)

    # cryptopia check
    if recipient_input == "edf2d63cdf0b6275ead22c9e6d66aa8ea31dc0ccb367fad2e7c08a25" and len(openfield_input) not in [16, 20]:
        messagebox.showinfo("Cannot send", "Identification message is missing for Cryptopia, please include it")
        return
    # cryptopia check

    top10 = Toplevel()
    top10.title("Confirm")

    if alias_cb_var.get() == True:  # alias check
        connections.send(s, "addfromalias", 10)
        connections.send(s, recipient_input, 10)
        recipient_input = connections.receive(s, 10)

    # encr check
    if encrypt_var.get() == True:
        # get recipient's public key

        connections.send(s, "pubkeyget", 10)
        connections.send(s, recipient_input, 10)
        target_public_key_hashed = connections.receive(s, 10)

        recipient_key = RSA.importKey(base64.b64decode(target_public_key_hashed).decode("utf-8"))

        # openfield_input = str(target_public_key.encrypt(openfield_input.encode("utf-8"), 32))

        data = openfield_input.encode("utf-8")
        # print (open("pubkey.der").read())
        session_key = get_random_bytes(16)
        cipher_aes = AES.new(session_key, AES.MODE_EAX)

        # Encrypt the session key with the public RSA key
        cipher_rsa = PKCS1_OAEP.new(recipient_key)

        # Encrypt the data with the AES session key
        ciphertext, tag = cipher_aes.encrypt_and_digest(data)
        enc_session_key = (cipher_rsa.encrypt(session_key))
        openfield_input = str([x for x in (cipher_aes.nonce, tag, ciphertext, enc_session_key)])

    # encr check

    if encode_var.get() == True and msg_var.get() == False:
        openfield_input = base64.b64encode(openfield_input.encode("utf-8")).decode("utf-8")
    if msg_var.get() == True and encode_var.get() == True:
        openfield_input = "bmsg=" + base64.b64encode(openfield_input.encode("utf-8")).decode("utf-8")
    if msg_var.get() == True and encode_var.get() == False:
        openfield_input = "msg=" + openfield_input
    if encrypt_var.get() == True:
        openfield_input = "enc=" + str(openfield_input)

    fee = fee_calculate(openfield_input)

    confirmation_dialog = Text(top10, width=100)
    confirmation_dialog.insert(INSERT, ("Amount: {}\nFee: {}\nTotal: {}\nTo: {}\nOpenField:\n\n{}".format('{:.8f}'.format(amount_input), '{:.8f}'.format(fee), '{:.8f}'.format(Decimal(amount_input) + Decimal(fee)), recipient_input, openfield_input)))
    confirmation_dialog.configure(state="disabled")
    confirmation_dialog.grid(row=0, pady=0)

    enter = Button(top10, text="Confirm", command=lambda: send_confirmed(amount_input, recipient_input, openfield_input, top10))
    enter.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))

    done = Button(top10, text="Cancel", command=top10.destroy)
    done.grid(row=2, column=0, sticky=W + E, padx=15, pady=(5, 5))


def send_confirmed(amount_input, recipient_input, openfield_input, top10):
    send(amount_input, recipient_input, openfield_input)
    top10.destroy()


def send(amount_input, recipient_input, openfield_input):
    all_spend_check()

    if key is None:
        top5 = Toplevel()
        top5.title("Locked")

        Label(top5, text="Wallet is locked", width=20).grid(row=0, pady=0)

        done = Button(top5, text="Cancel", command=top5.destroy)
        done.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))

    app_log.warning("Received tx command")

    try:
        Decimal(amount_input)
    except:
        top7 = Toplevel()
        top7.title("Invalid amount")
        Label(top7, text="Amount must be a number", width=20).grid(row=0, pady=0)
        done = Button(top7, text="Cancel", command=top7.destroy)
        done.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))

    # alias check

    # alias check

    if not address_validate(recipient_input):
        top6 = Toplevel()
        top6.title("Invalid address")
        Label(top6, text="Wrong address format", width=20).grid(row=0, pady=0)
        done = Button(top6, text="Cancel", command=top6.destroy)
        done.grid(row=1, column=0, sticky=W + E, padx=15, pady=(5, 5))
    else:

        app_log.warning("Amount: {}".format(amount_input))
        app_log.warning("Recipient: {}".format(recipient_input))
        app_log.warning("OpenField Data: {}".format(openfield_input))

        timestamp = '%.2f' % time.time()
        operation_input = "0"
        transaction = (str(timestamp), str(myaddress), str(recipient_input), '%.8f' % float(amount_input), str(operation_input), str(openfield_input))  # this is signed, float kept for compatibility

        h = SHA.new(str(transaction).encode("utf-8"))
        signer = PKCS1_v1_5.new(key)
        signature = signer.sign(h)
        signature_enc = base64.b64encode(signature)
        app_log.warning("Client: Encoded Signature: {}".format(signature_enc.decode("utf-8")))

        verifier = PKCS1_v1_5.new(key)
        if verifier.verify(h, signature) == True:
            fee = fee_calculate(openfield_input)

            if Decimal(amount_input) < 0:
                app_log.warning("Client: Signature OK, but cannot use negative amounts")

            elif (Decimal(amount_input) + Decimal(fee) > Decimal(balance)):
                print(amount_input, fee, balance)
                app_log.warning("Mempool: Sending more than owned")

            else:
                app_log.warning("Client: The signature is valid, proceeding to save transaction, signature, new txhash and the public key to mempool")

                # print(str(timestamp), str(address), str(recipient_input), '%.8f' % float(amount_input),str(signature_enc), str(public_key_hashed), str(keep_input), str(openfield_input))
                tx_submit = str(timestamp), str(myaddress), str(recipient_input), '%.8f' % float(amount_input), str(signature_enc.decode("utf-8")), str(public_key_hashed.decode("utf-8")), str(operation_input), str(openfield_input)  # float kept for compatibility

                while True:
                    connections.send(s, "mpinsert", 10)
                    connections.send(s, tx_submit, 10)
                    reply = connections.receive(s, 10)
                    app_log.warning("Client: {}".format(reply))
                    break

                refresh(gui_address.get(), s)
        else:
            app_log.warning("Client: Invalid signature")
        # enter transaction end


# def app_quit():
#    app_log.warning("Received quit command")
#    root.destroy()


def qr(address):
    address_qr = pyqrcode.create(address)
    address_qr.png('address_qr.png')

    # popup
    top = Toplevel()
    top.title("Address QR Code")

    im = PIL.Image.open("address_qr.png")

    photo = PIL.ImageTk.PhotoImage(im.resize((320, 320)))
    label = Label(top, image=photo)
    label.image = photo  # keep a reference!
    label.pack()

    # msg = Message(top, text="hi")
    # msg.pack()

    button = Button(top, text="Dismiss", command=top.destroy)
    button.pack()
    # popup


def msg_dialogue(address):
    connections.send(s, "addlist", 10)
    connections.send(s, myaddress, 10)
    addlist = connections.receive(s, 10)
    print(addlist)

    def msg_received_get(addlist):

        for x in addlist:
            if x[11].startswith(("msg=", "bmsg=", "enc=msg=", "enc=bmsg=")) and x[3] == address:
                # print(x[11])

                connections.send(s, "aliasget", 10)
                connections.send(s, x[2], 10)

                msg_address = connections.receive(s, 10)[0][0]

                if x[11].startswith("enc=msg="):
                    msg_received_digest = replace_regex(x[11], "enc=msg=")
                    try:
                        # msg_received_digest = key.decrypt(ast.literal_eval(msg_received_digest)).decode("utf-8")

                        (cipher_aes_nonce, tag, ciphertext, enc_session_key) = ast.literal_eval(msg_received_digest)
                        private_key = RSA.import_key(open("privkey.der").read())
                        # Decrypt the session key with the public RSA key
                        cipher_rsa = PKCS1_OAEP.new(private_key)
                        session_key = cipher_rsa.decrypt(enc_session_key)
                        # Decrypt the data with the AES session key
                        cipher_aes = AES.new(session_key, AES.MODE_EAX, cipher_aes_nonce)
                        msg_received_digest = cipher_aes.decrypt_and_verify(ciphertext, tag).decode("utf-8")

                    except:
                        msg_received_digest = "Could not decrypt message"

                elif x[11].startswith("enc=bmsg="):
                    msg_received_digest = replace_regex(x[11], "enc=bmsg=")
                    try:
                        msg_received_digest = base64.b64decode(msg_received_digest).decode("utf-8")

                        # msg_received_digest = key.decrypt(ast.literal_eval(msg_received_digest)).decode("utf-8")
                        (cipher_aes_nonce, tag, ciphertext, enc_session_key) = ast.literal_eval(msg_received_digest)
                        private_key = RSA.import_key(open("privkey.der").read())
                        # Decrypt the session key with the public RSA key
                        cipher_rsa = PKCS1_OAEP.new(private_key)
                        session_key = cipher_rsa.decrypt(enc_session_key)
                        # Decrypt the data with the AES session key
                        cipher_aes = AES.new(session_key, AES.MODE_EAX, cipher_aes_nonce)
                        msg_received_digest = cipher_aes.decrypt_and_verify(ciphertext, tag).decode("utf-8")

                    except:
                        msg_received_digest = "Could not decrypt message"


                elif x[11].startswith("bmsg="):
                    msg_received_digest = replace_regex(x[11], "bmsg=")
                    try:
                        msg_received_digest = base64.b64decode(msg_received_digest).decode("utf-8")
                    except:
                        msg_received_digest = "Could not decode message"
                elif x[11].startswith("msg="):
                    msg_received_digest = replace_regex(x[11], "msg=")

                msg_received.insert(INSERT, ((time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(Decimal(x[1])))) + " From " + replace_regex(msg_address, "alias=") + ": " + msg_received_digest) + "\n")

    def msg_sent_get(addlist):

        for x in addlist:
            if x[11].startswith(("msg=", "bmsg=", "enc=msg=", "enc=bmsg=")) and x[2] == address:
                # print(x[11])

                connections.send(s, "aliasget", 10)
                connections.send(s, x[3], 10)
                received_aliases = connections.receive(s, 10)
                msg_recipient = received_aliases[0][0]

                if x[11].startswith("enc=msg="):
                    msg_sent_digest = replace_regex(x[11], "enc=msg=")
                    try:
                        # msg_sent_digest = key.decrypt(ast.literal_eval(msg_sent_digest)).decode("utf-8")
                        (cipher_aes_nonce, tag, ciphertext, enc_session_key) = ast.literal_eval(msg_sent_digest)
                        private_key = RSA.import_key(open("privkey.der").read())
                        # Decrypt the session key with the public RSA key
                        cipher_rsa = PKCS1_OAEP.new(private_key)
                        session_key = cipher_rsa.decrypt(enc_session_key)
                        # Decrypt the data with the AES session key
                        cipher_aes = AES.new(session_key, AES.MODE_EAX, cipher_aes_nonce)
                        msg_sent_digest = cipher_aes.decrypt_and_verify(ciphertext, tag).decode("utf-8")

                    except:
                        msg_sent_digest = "Could not decrypt message"

                elif x[11].startswith("enc=bmsg="):
                    msg_sent_digest = replace_regex(x[11], "enc=bmsg=")
                    try:
                        msg_sent_digest = base64.b64decode(msg_sent_digest).decode("utf-8")
                        # msg_sent_digest = key.decrypt(ast.literal_eval(msg_sent_digest)).decode("utf-8")
                        (cipher_aes_nonce, tag, ciphertext, enc_session_key) = ast.literal_eval(msg_sent_digest)
                        private_key = RSA.import_key(open("privkey.der").read())
                        # Decrypt the session key with the public RSA key
                        cipher_rsa = PKCS1_OAEP.new(private_key)
                        session_key = cipher_rsa.decrypt(enc_session_key)
                        # Decrypt the data with the AES session key
                        cipher_aes = AES.new(session_key, AES.MODE_EAX, cipher_aes_nonce)
                        msg_sent_digest = cipher_aes.decrypt_and_verify(ciphertext, tag).decode("utf-8")
                    except:
                        msg_sent_digest = "Could not decrypt message"

                elif x[11].startswith("bmsg="):
                    msg_sent_digest = replace_regex(x[11], "bmsg=")
                    try:
                        msg_sent_digest = base64.b64decode(msg_sent_digest).decode("utf-8")
                    except:
                        msg_received_digest = "Could not decode message"

                elif x[11].startswith("msg="):
                    msg_sent_digest = replace_regex(x[11], "msg=")

                msg_sent.insert(INSERT, ((time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(Decimal(x[1])))) + " To " + replace_regex(msg_recipient, "alias=") + ": " + msg_sent_digest) + "\n")

    # popup
    top11 = Toplevel()
    top11.title("Messaging")

    Label(top11, text="Received:", width=20).grid(row=0)

    msg_received = Text(top11, width=100, height=20, font=("Tahoma", 8))
    msg_received.grid(row=1, column=0, sticky=W, padx=5, pady=(5, 5))
    msg_received_get(addlist)

    Label(top11, text="Sent:", width=20).grid(row=2)

    msg_sent = Text(top11, width=100, height=20, font=("Tahoma", 8))
    msg_sent.grid(row=3, column=0, sticky=W, padx=5, pady=(5, 5))
    msg_sent_get(addlist)

    dismiss = Button(top11, text="Dismiss", command=top11.destroy)
    dismiss.grid(row=5, column=0, sticky=W + E, padx=15, pady=(5, 5))

    # popup

def refresh_auto():
    root.after(0, refresh(gui_address.get(), s))
    root.after(10000, refresh_auto)



def stats():
    stats_window = Toplevel ()
    stats_window.title ("Node Statistics")
    stats_window.resizable (0, 0)


    #canvas_stats_bg = Canvas (root, highlightthickness=0)
    #canvas_stats_bg.grid (row=0, column=0, rowspan=200, columnspan=200, sticky=W + E + S + N)

    #stats_window.update ()
    #width_stats = stats_window.winfo_width ()
    #height_stats = stats_window.winfo_height ()

    #img_stats_bg = PhotoImage (file="graphics/brushed.png")
    #canvas_bg.create_image (width_stats, height_stats, image=img_stats_bg)



    frame_chart = Frame (stats_window, height=100, width=100)
    frame_chart.grid (row=0, column=1, rowspan=999)
    f = Figure (figsize=(11, 7), dpi=100)
    f.set_facecolor ('silver')
    f.subplots_adjust (left=None, bottom=None, right=None, top=None, wspace=None, hspace=0.5)

    def chart_fill():
        f.clear()

        rows = 4
        columns = 2

        first = f.add_subplot (rows, columns, 1)
        first.plot ((range (len (stats_nodes_count_list))), (stats_nodes_count_list))
        first.ticklabel_format (useOffset=False)

        first_2 = f.add_subplot (rows, columns, 1)
        first_2.plot ((range (len (stats_thread_count_list))), (stats_thread_count_list))
        first_2.ticklabel_format (useOffset=False)
        first.legend (('Nodes', 'Threads'), loc='best', shadow=True)

        second = f.add_subplot (rows, columns, 2)
        second.plot ((range (len (stats_consensus_list))), (stats_consensus_list))
        second.legend (('Consensus Block',), loc='best', shadow=True)
        second.ticklabel_format (useOffset=False)

        third = f.add_subplot (rows, columns, 3)
        third.plot ((range (len (stats_consensus_percentage_list))), (stats_consensus_percentage_list))
        third.legend (('Consensus Level',), loc='best', shadow=True)
        third.ticklabel_format (useOffset=False)

        fourth = f.add_subplot (rows, columns, 4)
        fourth.plot ((range (len (stats_diff_list_2))), (stats_diff_list_2))
        fourth.legend (('Time To Generate Block',), loc='best', shadow=True)
        fourth.ticklabel_format (useOffset=False)

        fifth = f.add_subplot (rows, columns, 5)
        fifth.plot ((range (len (stats_diff_list_0))), (stats_diff_list_0))
        fifth.ticklabel_format (useOffset=False)

        fifth_2 = f.add_subplot (rows, columns, 5)
        fifth_2.plot ((range (len (stats_diff_list_1))), (stats_diff_list_1))
        fifth_2.ticklabel_format (useOffset=False)

        fifth_3 = f.add_subplot (rows, columns, 5)
        fifth_3.plot ((range (len (stats_diff_list_3))), (stats_diff_list_3))
        fifth_3.ticklabel_format (useOffset=False)
        fifth.legend (('Diff 1', 'Diff 2', 'Diff Current',), loc='best', shadow=True)

        sixth = f.add_subplot (rows, columns, 6)
        sixth.plot ((range (len (stats_diff_list_4))), (stats_diff_list_4))
        sixth.legend (('Block Time',), loc='best', shadow=True)
        sixth.ticklabel_format (useOffset=False)

        seventh = f.add_subplot (rows, columns, 7)
        seventh.plot ((range (len (stats_diff_list_5))), (stats_diff_list_5))
        seventh.legend (('Hashrate',), loc='best', shadow=True)
        seventh.ticklabel_format (useOffset=False)

        eigth = f.add_subplot (rows, columns, 8)
        eigth.plot ((range (len (stats_diff_list_6))), (stats_diff_list_6))
        eigth.legend (('Difficulty Adjustment',), loc='best', shadow=True)
        eigth.ticklabel_format (useOffset=False)

        # a tk.DrawingArea
        canvas = FigureCanvasTkAgg (f, master=frame_chart)
        canvas.draw ()
        canvas.get_tk_widget ().grid (row=0, column=1, sticky=W, padx=15, pady=(0, 0))

    def update():
        print("Statistics update triggered")
        stats_address = statusget[0]
        stats_nodes_count = statusget[1]
        stats_nodes_list = statusget[2]
        stats_thread_count = statusget[3]
        stats_uptime = statusget[4]
        stats_consensus = statusget[5]
        stats_consensus_percentage = statusget[6]
        stats_version = statusget[7]
        stats_diff = statusget[8]

        stats_address_label_var.set ("Node Address: {}".format (stats_address))
        stats_nodes_count_label_var.set ("Number of Nodes: {}".format (stats_nodes_count))
        for entry in stats_nodes_list:
            stats_nodes_list_text_var.insert (END, entry)
        stats_nodes_list_text_var.grid (row=2, column=0, sticky=E, padx=15, pady=(0, 0))

        stats_thread_count_var.set ("Number of Threads: {}".format (stats_thread_count))
        stats_uptime_var.set ("Uptime: {:.2f} hours".format (stats_uptime / 60 / 60))
        stats_consensus_var.set ("Consensus Block: {}".format (stats_consensus))
        stats_consensus_consensus_percentage_var.set ("Consensus Level: {:.2f}%".format (stats_consensus_percentage))
        stats_version_var.set ("Version: {}".format (stats_version))
        stats_diff_var_0.set ("Difficulty 1: {}".format (stats_diff[0]))
        stats_diff_var_1.set ("Difficulty 2: {}".format (stats_diff[1]))
        stats_diff_var_2.set ("Time to Generate Block: {}".format (stats_diff[2]))
        stats_diff_var_3.set ("Current Block Difficulty: {}".format (stats_diff[3]))
        stats_diff_var_4.set ("Block Time: {}".format (stats_diff[4]))
        stats_diff_var_5.set ("Hashrate: {}".format (stats_diff[5]))
        stats_diff_var_6.set ("Difficulty Adjustment: {}".format (stats_diff[6]))


    stats_address_label_var = StringVar()
    stats_address_label = Label(stats_window, textvariable=stats_address_label_var)
    stats_address_label.grid(row=0, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_nodes_count_label_var = StringVar()
    stats_nodes_count_label = Label(stats_window, textvariable=stats_nodes_count_label_var)
    stats_nodes_count_label.grid(row=1, column=0, sticky=E, padx=15, pady=(0, 0))


    scrollbar = Scrollbar (stats_window)
    scrollbar.grid (row=2, column=0, sticky=N+S+E, padx=140)

    stats_nodes_list_text_var = Listbox (stats_window, width=20, height=10, font=("Tahoma", 8))


    scrollbar.config (command=stats_nodes_list_text_var.yview)

    stats_thread_count_var = StringVar()
    stats_thread_count_label = Label(stats_window, textvariable=stats_thread_count_var)
    stats_thread_count_label.grid(row=3, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_uptime_var = StringVar()
    stats_uptime_label = Label(stats_window, textvariable=stats_uptime_var)
    stats_uptime_label.grid(row=4, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_consensus_var = StringVar()
    stats_consensus_label = Label(stats_window, textvariable=stats_consensus_var)
    stats_consensus_label.grid(row=5, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_consensus_consensus_percentage_var = StringVar()
    stats_consensus_consensus_percentage_label = Label(stats_window, textvariable=stats_consensus_consensus_percentage_var)
    stats_consensus_consensus_percentage_label.grid(row=6, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_version_var = StringVar()
    stats_version_label = Label(stats_window, textvariable=stats_version_var)
    stats_version_label.grid(row=7, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_0 = StringVar()
    stats_diff_label_0 = Label(stats_window, textvariable=stats_diff_var_0)
    stats_diff_label_0.grid(row=8, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_1 = StringVar()
    stats_diff_label_1 = Label(stats_window, textvariable=stats_diff_var_1)
    stats_diff_label_1.grid(row=9, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_2 = StringVar()
    stats_diff_label_2 = Label(stats_window, textvariable=stats_diff_var_2)
    stats_diff_label_2.grid(row=10, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_3 = StringVar()
    stats_diff_label_3 = Label(stats_window, textvariable=stats_diff_var_3)
    stats_diff_label_3.grid(row=11, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_4 = StringVar()
    stats_diff_label_4 = Label(stats_window, textvariable=stats_diff_var_4)
    stats_diff_label_4.grid(row=12, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_5 = StringVar()
    stats_diff_label_5 = Label(stats_window, textvariable=stats_diff_var_5)
    stats_diff_label_5.grid(row=13, column=0, sticky=E, padx=15, pady=(0, 0))

    stats_diff_var_6 = StringVar()
    stats_diff_label_6 = Label(stats_window, textvariable=stats_diff_var_6)
    stats_diff_label_6.grid(row=14, column=0, sticky=E, padx=15, pady=(0, 0))

    def refresh_stats_auto():
        try:
            #global frame_chart
            root.after (0, update())
            root.after (10000, refresh_stats_auto)

            #frame_chart.destroy()
            chart_fill()
        except:
            print("Statistics window closed, disabling auto-refresh")

    refresh_stats_auto()





def csv_export(s):
    connections.send (s, "addlist", 10)  # senders
    connections.send (s, gui_address.get(), 10)

    tx_list = connections.receive (s, 10)
    print(tx_list)

    root.filename = filedialog.asksaveasfilename(initialdir="", title="Select CSV file", filetypes=(("CSV", "*.csv"),))

    with open (root.filename, 'w', newline='') as csvfile:
        for transaction in tx_list:

            writer = csv.writer (csvfile, quoting=csv.QUOTE_MINIMAL)
            writer.writerow ([transaction[0], transaction[1], transaction[3], transaction[4], transaction[5], transaction[6], transaction[7], transaction[8], transaction[9], transaction[10], transaction[11]])


    return

def token_transfer(token, amount, window):
    openfield.delete('1.0', END)  # remove previous
    openfield.insert(INSERT, "token:transfer:{}:{}".format(token, amount))
    window.destroy()


def token_issue(token, amount, window):
    openfield.delete('1.0', END)  # remove previous
    openfield.insert(INSERT, "token:issue:{}:{}".format(token, amount))
    recipient.delete(0, END)
    recipient.insert(INSERT, myaddress)
    window.destroy()
    send_confirm(amount, 0, "token:issue:{}:{}".format(token, amount))


def tokens():
    token_db = "static/index.db"
    tokens_update(token_db, "static/ledger.db", "normal", app_log)  # catch up with the chain

    address = gui_address.get()
    tokens_main = Toplevel()
    tokens_main.title("Tokens")

    tok = sqlite3.connect(token_db)
    tok.text_factory = str
    t = tok.cursor()

    t.execute("SELECT DISTINCT token FROM tokens WHERE address OR recipient = ?", (address,))
    tokens_user = t.fetchall()
    print("tokens_user", tokens_user)

    token_box = Listbox(tokens_main, width=100)
    token_box.grid(row=0, pady=0)

    for token in tokens_user:
        token = token[0]
        t.execute("SELECT sum(amount) FROM tokens WHERE recipient = ? AND token = ?;", (address,) + (token,))
        credit = t.fetchone()[0]
        t.execute("SELECT sum(amount) FROM tokens WHERE address = ? AND token = ?;", (address,) + (token,))
        debit = t.fetchone()[0]

        debit = 0 if debit is None else debit
        credit = 0 if credit is None else credit

        balance = Decimal(credit) - Decimal(debit)

        token_box.insert(END, (token, ":", balance))

    # callback
    def callback(event):
        token_select = (token_box.get(token_box.curselection()[0]))
        token_name_var.set(token_select[0])
        token_amount_var.set(token_select[2])

    token_box.bind('<Double-1>', callback)

    # callback

    token_name_var = StringVar()
    token_name = Entry(tokens_main, textvariable=token_name_var, width=80)
    token_name.grid(row=2, column=0, sticky=E, padx=15, pady=(5, 5))

    token_name_label_var = StringVar()
    token_name_label_var.set("Token Name:")
    token_name_label = Label(tokens_main, textvariable=token_name_label_var)
    token_name_label.grid(row=2, column=0, sticky=W, padx=15, pady=(0, 0))

    # balance_var = StringVar()
    # balance_msg_label = Label(frame_buttons, textvariable=balance_var)

    token_amount_var = StringVar()
    token_amount = Entry(tokens_main, textvariable=token_amount_var, width=80, )
    token_amount.grid(row=3, column=0, sticky=E, padx=15, pady=(5, 5))

    token_amount_label_var = StringVar()
    token_amount_label_var.set("Token Amount:")
    token_amount_label = Label(tokens_main, textvariable=token_amount_label_var)
    token_amount_label.grid(row=3, column=0, sticky=W, padx=15, pady=(0, 0))

    transfer = Button(tokens_main, text="Transfer", command=lambda: token_transfer(token_name_var.get(), token_amount_var.get(), tokens_main))
    transfer.grid(row=4, column=0, sticky=W + E, padx=5)

    issue = Button(tokens_main, text="Issue", command=lambda: token_issue(token_name_var.get(), token_amount_var.get(), tokens_main))
    issue.grid(row=5, column=0, sticky=W + E, padx=5)

    cancel = Button(tokens_main, text="Cancel", command=tokens_main.destroy)
    cancel.grid(row=6, column=0, sticky=W + E, padx=5)


def table(address, addlist_20, mempool_total):
    # transaction table
    # data

    datasheet = ["Time", "From", "To", "Amount", "Type"]

    # show mempool txs

    colors = []

    for tx in mempool_total:
        if tx[1] == address:
            datasheet.append("Unconfirmed")
            datasheet.append(tx[1])
            datasheet.append(tx[2])
            datasheet.append(tx[3])
            datasheet.append("Transaction")
            colors.append("bisque")

    # show mempool txs

    # retrieve aliases in bulk

    addlist_addressess = []
    reclist_addressess = []

    for x in addlist_20:
        addlist_addressess.append(x[2])  # append address
        reclist_addressess.append(x[3])  # append recipient
    # print(addlist_addressess)

    # define row color

    for x in addlist_20:
        if x[3] == address:
            colors.append("green4")
        else:
            colors.append("indianred")
    # define row color

    if resolve_var.get() == 1:
        connections.send(s, "aliasesget", 10)  # senders
        connections.send(s, addlist_addressess, 10)
        aliases_address_results = connections.receive(s, 10)
        # print(aliases_address_results)

        connections.send(s, "aliasesget", 10)  # recipients
        connections.send(s, reclist_addressess, 10)
        aliases_rec_results = connections.receive(s, 10)
        # print(aliases_rec_results)
    # retrieve aliases in bulk

    i = 0
    for row in addlist_20:

        db_timestamp = row[1]
        datasheet.append(datetime.fromtimestamp(Decimal(db_timestamp)).strftime('%Y-%m-%d %H:%M:%S'))

        if resolve_var.get() == True:
            db_address = replace_regex(aliases_address_results[i], "alias=")
        else:
            db_address = row[2]

        datasheet.append(db_address)

        if resolve_var.get() == True:
            db_recipient = replace_regex(aliases_rec_results[i], "alias=")

        else:
            db_recipient = row[3]

        datasheet.append(db_recipient)

        db_amount = row[4]
        db_reward = row[9]
        db_openfield = row[11]

        datasheet.append(db_amount + db_reward)
        if Decimal(db_reward) > 0:
            symbol = "Mined"
        elif db_openfield.startswith("bmsg"):
            symbol = "b64 Message"
        elif db_openfield.startswith("msg"):
            symbol = "Message"
        else:
            symbol = "Transaction"
        datasheet.append(symbol)

        i = i + 1
    # data

    app_log.warning(datasheet)
    app_log.warning(len(datasheet))

    if len(datasheet) == 5:
        app_log.warning("Looks like a new address")

    elif len(datasheet) < 20 * 5:
        app_log.warning(len(datasheet))
        table_limit = len(datasheet) / 5
    else:
        table_limit = 20

    if len(datasheet) > 5:
        k = 0

        for child in frame_table.winfo_children():  # prevent hangup
            child.destroy()

        for i in range(int(table_limit)):
            for j in range(5):
                datasheet_compare = [datasheet[k], datasheet[k - 1], datasheet[k - 2], datasheet[k - 3], datasheet[k - 4]]

                if "Time" in datasheet_compare:  # header
                    e = Entry(frame_table, width=0)
                    e.configure(readonlybackground='linen')

                elif j == 0:  # first row
                    e = Entry(frame_table, width=0)
                    e.configure(readonlybackground='linen')

                elif "Unconfirmed" in datasheet_compare:  # unconfirmed txs
                    e = Entry(frame_table, width=0)
                    e.configure(readonlybackground='linen')

                elif j == 3:  # sent
                    e = Entry(frame_table, width=0)
                    e.configure(readonlybackground=colors[i - 1])

                elif j == 4:  # last row
                    e = Entry(frame_table, width=0)
                    e.configure(readonlybackground='bisque')

                else:
                    e = Entry(frame_table, width=56)
                    e.configure(readonlybackground='bisque')

                e.grid(row=i + 1, column=j, sticky=EW)
                e.insert(END, datasheet[k])
                e.configure(state="readonly")

                k = k + 1

                # transaction table
                # refreshables



def refresh(address, s):
    global balance
    global statusget
    global block_height_old

    # print "refresh triggered"
    try:

        connections.send(s, "statusget", 10)
        statusget = connections.receive(s, 10)
        status_version = statusget[7]

        # data for charts
        print(statusget)
        block_height = statusget[8][7] #move chart only if the block height changes, returned from diff 7
        try:
            block_height_old
        except:
            block_height_old = block_height #init

        if block_height_old != block_height or not stats_nodes_count_list: #or if list is empty
            print("Chart update in progress")

            stats_nodes_count_list.append (statusget[1])
            stats_thread_count_list.append (statusget[3])
            stats_consensus_list.append (statusget[5])
            stats_consensus_percentage_list.append (statusget[6])


            stats_diff_list_0.append (statusget[8][0])
            stats_diff_list_1.append (statusget[8][1])
            stats_diff_list_2.append (statusget[8][2])
            stats_diff_list_3.append (statusget[8][3])
            stats_diff_list_4.append (statusget[8][4])
            stats_diff_list_5.append (statusget[8][5])
            stats_diff_list_6.append (statusget[8][6])

            block_height_old = block_height
        else:
            print("Chart update skipped, block hasn't moved")
        # data for charts

        connections.send(s, "balanceget", 10)
        connections.send(s, address, 10)  # change address here to view other people's transactions
        stats_account = connections.receive(s, 10)
        balance = stats_account[0]
        credit = stats_account[1]
        debit = stats_account[2]
        fees = stats_account[3]
        rewards = stats_account[4]

        app_log.warning("Transaction address balance: {}".format(balance))

        connections.send(s, "blocklast", 10)
        block_get = connections.receive(s, 10)
        bl_height = block_get[0]
        db_timestamp_last = block_get[1]
        hash_last = block_get[7]

        # check difficulty
        connections.send(s, "diffget", 10)
        diff = connections.receive(s, 10)
        # check difficulty

        print(diff)
        diff_msg = int(diff[1])  # integer is enough

        # network status
        time_now = str(time.time())
        last_block_ago = Decimal(time_now) - Decimal(db_timestamp_last)
        if last_block_ago > 300:
            sync_msg = "{}m behind".format((int(last_block_ago / 60)))
            sync_msg_label.config(fg='red')
        else:
            sync_msg = "Last block: {}s ago".format((int(last_block_ago)))
            sync_msg_label.config(fg='green')

        # network status

        connections.send(s, "mpget", 10)  # senders
        mempool_total = connections.receive(s, 10)
        print(mempool_total)

        # fees_current_var.set("Current Fee: {}".format('%.8f' % float(fee)))
        balance_var.set("Balance: {:.8f}".format(Decimal(balance)))
        balance_raw.set(balance)
        debit_var.set("Spent Total: {:.8f}".format(Decimal(debit)))
        credit_var.set("Received Total: {:.8f}".format(Decimal(credit)))
        fees_var.set("Fees Paid: {:.8f}".format(Decimal(fees)))
        rewards_var.set("Rewards: {:.8f}".format(Decimal(rewards)))
        bl_height_var.set("Block Height: {}".format(bl_height))
        diff_msg_var.set("Mining Difficulty: {}".format(diff_msg))
        sync_msg_var.set(sync_msg)
        version_var.set("Local Version: {}".format(status_version))
        hash_var.set("Hash: {}...".format(hash_last[:6]))
        mempool_count_var.set("Mempool txs: {}".format(len(mempool_total)))

        connections.send(s, "annverget", 10)
        annverget = connections.receive(s, 10)
        current_var.set(annverget)
        current_var.set("Latest version: {}".format(annverget))

        if status_version != annverget:
            version_color = "red"
        else:
            version_color = "green"
        version_var_label.config (fg=version_color)


        connections.send(s, "addlistlim", 10)
        connections.send(s, address, 10)
        connections.send(s, "20", 10)
        addlist = connections.receive(s, 10)
        addlist_20 = addlist[:20]  # limit

        table(address, addlist_20, mempool_total)
        # root.after(1000, refresh)

        connections.send(s, "annget", 10)
        annget = connections.receive(s, 10)
        ann_var_text.config (state=NORMAL)
        ann_var_text.delete('1.0', END)
        ann_var_text.insert (INSERT, annget)
        ann_var_text.config (state=DISABLED)

        all_spend_check()

    except:
        node_connect()


def sign():
    def verify_this():
        try:
            received_public_key = RSA.importKey(public_key_gui.get("1.0", END))
            verifier = PKCS1_v1_5.new(received_public_key)
            h = SHA.new(input_text.get("1.0", END).encode("utf-8"))
            received_signature_dec = base64.b64decode(output_signature.get("1.0", END))

            if verifier.verify(h, received_signature_dec) == True:
                top2 = Toplevel()
                top2.title("Validation results")
                msg = Message(top2, text="Signature Valid", width=50)
                msg.pack()
                button = Button(top2, text="Dismiss", command=top2.destroy)
                button.pack()
            else:
                raise
        except:
            top2 = Toplevel()
            top2.title("Validation results")
            msg = Message(top2, text="Signature Invalid", width=50)
            msg.pack()
            button = Button(top2, text="Dismiss", command=top2.destroy)
            button.pack()

    def sign_this():
        h = SHA.new(input_text.get("1.0", END).encode("utf-8"))
        signer = PKCS1_v1_5.new(key)
        signature = signer.sign(h)
        signature_enc = base64.b64encode(signature)

        output_signature.delete('1.0', END)  # remove previous
        output_signature.insert(INSERT, signature_enc)

    # popup
    top = Toplevel()
    top.title("Sign message")
    # top.geometry("%dx%d%+d%+d" % (800, 600, 0, 0))
    # top.grid_propagate(False)

    Label(top, text="Message:", width=20).grid(row=0, pady=0)
    input_text = Text(top, height=10)
    # label.image = photo  # keep a reference!
    input_text.grid(row=1, column=0, sticky=N + E, padx=15, pady=(0, 0))

    Label(top, text="Public Key:", width=20).grid(row=2, pady=0)
    public_key_gui = Text(top, height=10)
    public_key_gui.insert(INSERT, public_key_readable)
    public_key_gui.grid(row=3, column=0, sticky=N + E, padx=15, pady=(0, 0))

    Label(top, text="Signature:", width=20).grid(row=4, pady=0)
    output_signature = Text(top, height=10)
    output_signature.grid(row=5, column=0, sticky=N + E, padx=15, pady=(0, 0))

    # msg = Message(top, text="hi")
    # msg.pack()

    sign_message = Button(top, text="Sign Message", command=sign_this)
    sign_message.grid(row=6, column=0, sticky=W + E, padx=15, pady=(5, 0))

    sign_message = Button(top, text="Verify Message", command=verify_this)
    sign_message.grid(row=7, column=0, sticky=W + E, padx=15, pady=(15, 0))

    dismiss = Button(top, text="Dismiss", command=top.destroy)
    dismiss.grid(row=8, column=0, sticky=W + E, padx=15, pady=(15, 5))
    # popup

root = Tk()


root.wm_title("Bismuth Light Wallet running on {}".format(light_ip))
#root.geometry("1310x700") #You want the size of the app to be 500x500
root.resizable(0, 0) #Don't allow resizing in the x or y direction / resize
root['bg']="black"




img = Image("photo", file="graphics/icon.gif")
root.tk.call('wm', 'iconphoto', root._w, img, )




if gui_scaling == "adapt":
    dpi_value = root.winfo_fpixels('1i')
    root.tk.call ('tk', 'scaling', dpi_value / 72)

elif gui_scaling != "default":
    root.tk.call("tk", "scaling", gui_scaling)

#root.tk.call("tk", "scaling", 2)#test

password_var_enc = StringVar()
password_var_con = StringVar()
password_var_dec = StringVar()

canvas_bg = Canvas(root,highlightthickness=0)
canvas_bg.grid(row=0, column=0, rowspan=200,columnspan=200,sticky=W + E + S + N)

frame_logo = Frame(root, relief = 'ridge', borderwidth = 4)
frame_logo.grid(row=0, column=1, pady=5, padx=5)

frame_main = Frame(root,relief = 'ridge', borderwidth = 4)
frame_main.grid(row=0, column=0, pady=5, padx=5)
canvas_main = Canvas(frame_main,highlightthickness=0,height=0) #height=0 = hack to prevent resizing
canvas_main.grid(row=0, column=0, sticky=W + E + N + S, columnspan=3,rowspan=2)

frame_labels = Frame(frame_main,relief = 'ridge', borderwidth = 4)
frame_labels.grid(row=0, column=0, pady=5, padx=5, sticky=N+W+E+S)

frame_entries = Frame(frame_main,relief = 'ridge', borderwidth = 4)
frame_entries.grid(row=0, column=1, pady=5, padx=5)

frame_tick = Frame(frame_main,relief = 'ridge', borderwidth = 4)
frame_tick.grid(row=0, column=2, pady=5, padx=5)

#spacer_width=150
#frame_entries_spacer = Frame(frame_entries, width=spacer_width).grid(row=0,sticky=NW) #make canvas text visible

frame_table = Frame(root,relief = 'ridge', borderwidth = 4)
frame_table.grid(row=1, column=0, sticky=W + E + N, pady=5, padx=5)

frame_buttons = Frame(root, relief = 'ridge', borderwidth = 4)
frame_buttons.grid(row=1, column=1, sticky=W + E + N + S, pady=5, padx=5)


# frames
# menu
def hello():
    pass

#
root.update()
width_root = root.winfo_width()
height_root = root.winfo_height()

frame_main.update()
width_main = frame_main.winfo_width()
height_main = frame_main.winfo_height()

print(height_main, width_main)
print(height_root, width_root)
#


#canvas

img_bg = PIL.Image.open("graphics/bg.jpg")

photo_bg = PIL.ImageTk.PhotoImage(img_bg)
canvas_bg.create_image(0,0, image=photo_bg, anchor=NW)

main_bg = PIL.Image.open("graphics/main.jpg")
photo_main = PIL.ImageTk.PhotoImage(main_bg)
canvas_main.create_image(0,0, image=photo_main, anchor=NW)

Label(frame_labels, text="Address:",bg="yellow", height=2).grid(row=0,sticky=E+N)
Label(frame_labels, text="Recipient:", height=2).grid(row=1,sticky=E)
Label(frame_labels, text="Amount:",bg="yellow", height=2).grid(row=2,sticky=E)
Label(frame_labels, text="Data:",height=4).grid(row=3,sticky=E)
Label(frame_labels, text="URL:",bg="yellow", height=2).grid(row=4,sticky=E+S)
#canvas

menubar = Menu(root)
menubar.add_command(label="Exit", command=root.quit)
menubar.add_command(label="Help", command=help)

# display the menu
root.config(menu=menubar)
# menu

# buttons

button_row_zero = 0
column = 1

send_b = Button(frame_buttons, text="Send", command=lambda: send_confirm(str(amount.get()).strip(), recipient.get().strip(), (openfield.get("1.0", END)).strip()), height=1, width=20, font=("Tahoma", 8))
send_b.grid(row=button_row_zero, column=column, sticky=N + E, pady=0, padx=15)

qr_b = Button(frame_buttons, text="URL QR", command=lambda: qr(url.get()), height=1, width=20, font=("Tahoma", 8))
if "Linux" in platform.system():
    qr_b.configure(text="QR Disabled", state=DISABLED)
qr_b.grid(row=button_row_zero + 1, column=column, sticky=N + E, pady=0, padx=15)

message_b = Button(frame_buttons, text="Manual Refresh", command=lambda: refresh(gui_address.get(), s), height=1, width=20, font=("Tahoma", 8))
message_b.grid(row=button_row_zero + 2, column=column, sticky=N + E, pady=0, padx=15)

balance_b = Button(frame_buttons, text="Messages", command=lambda: msg_dialogue(gui_address.get()), height=1, width=20, font=("Tahoma", 8))
balance_b.grid(row=button_row_zero + 3, column=column, sticky=N + E, pady=0, padx=15)
# balance_b.configure(state=DISABLED)

sign_b = Button(frame_buttons, text="Sign Message", command=sign, height=1, width=20, font=("Tahoma", 8))
sign_b.grid(row=button_row_zero + 4, column=column, sticky=N + E, pady=0, padx=15)

alias_b = Button(frame_buttons, text="Alias Registration", command=alias, height=1, width=20, font=("Tahoma", 8))
alias_b.grid(row=button_row_zero + 5, column=column, sticky=N + E, pady=0, padx=15)

backup_b = Button(frame_buttons, text="Backup Wallet", command=keys_backup, height=1, width=20, font=("Tahoma", 8))
backup_b.grid(row=button_row_zero + 6, column=column, sticky=N + E, pady=0, padx=15)

load_b = Button(frame_buttons, text="Load Wallet", command=keys_load_dialog, height=1, width=20, font=("Tahoma", 8))
load_b.grid(row=button_row_zero + 7, column=column, sticky=N + E, pady=0, padx=15)

fingerprint_b = Button(frame_buttons, text="Fingerprint", command=fingerprint, height=1, width=20, font=("Tahoma", 8))
fingerprint_b.grid(row=button_row_zero + 8, column=column, sticky=N + E, pady=0, padx=15)

tokens_b = Button(frame_buttons, text="Tokens", command=tokens, height=1, width=20, font=("Tahoma", 8))
tokens_b.grid(row=button_row_zero + 9, column=column, sticky=N + E, pady=0, padx=15)

csv_export_b = Button(frame_buttons, text="CSV Export", command=lambda :csv_export(s), height=1, width=20, font=("Tahoma", 8))
csv_export_b.grid(row=button_row_zero + 10, column=column, sticky=N + E, pady=0, padx=15)

stats_b = Button(frame_buttons, text="Statistics", command=lambda :stats(), height=1, width=20, font=("Tahoma", 8))
stats_b.grid(row=button_row_zero + 11, column=column, sticky=N + E, pady=0, padx=15)

# quit_b = Button(frame_buttons, text="Quit", command=app_quit, height=1, width=10, font=("Tahoma", 8))
# quit_b.grid(row=16, column=0, sticky=W + E + S, pady=0, padx=15)

frame_logo_buttons = Frame(frame_logo, relief = 'ridge', borderwidth = 4)
frame_logo_buttons.grid(row=2, column=0, sticky=W + E, pady=5, padx=5)

encrypt_b = Button(frame_logo_buttons, text="Encrypt", command=encrypt_get_password, height=1, width=10)
encrypt_b.grid(row=0, column=0, sticky=E+W)
decrypt_b = Button(frame_logo_buttons, text="Unlock", command=decrypt_get_password, height=1, width=10)
decrypt_b.grid(row=0, column=1, sticky=E+W)
lock_b = Button(frame_logo_buttons, text="Locked", command=lambda: lock_fn(lock_b), height=1, width=10, state=DISABLED)
lock_b.grid(row=0, column=2, sticky=E+W)


def encryption_button_refresh():
    if unlocked == True:
        decrypt_b.configure(text="Unlocked", state=DISABLED)
    if unlocked == False:
        decrypt_b.configure(text="Unlock", state=NORMAL)
        sign_b.configure(text="Sign (locked)", state=DISABLED)
    if encrypted == False:
        encrypt_b.configure(text="Encrypt", state=NORMAL)
    if encrypted == True:
        encrypt_b.configure(text="Encrypted", state=DISABLED)


encryption_button_refresh()
# buttons

# refreshables

# update balance label
balance_raw = StringVar()
balance_var = StringVar()

balance_msg_label = Label(frame_buttons, textvariable=balance_var)
balance_msg_label.grid(row=0, column=0, sticky=N + E, padx=15)

debit_var = StringVar()
spent_msg_label = Label(frame_buttons, textvariable=debit_var)
spent_msg_label.grid(row=1, column=0, sticky=N + E, padx=15)

credit_var = StringVar()
received_msg_label = Label(frame_buttons, textvariable=credit_var)
received_msg_label.grid(row=2, column=0, sticky=N + E, padx=15)

fees_var = StringVar()
fees_paid_msg_label = Label(frame_buttons, textvariable=fees_var)
fees_paid_msg_label.grid(row=3, column=0, sticky=N + E, padx=15)

rewards_var = StringVar()
rewards_paid_msg_label = Label(frame_buttons, textvariable=rewards_var)
rewards_paid_msg_label.grid(row=4, column=0, sticky=N + E, padx=15)

bl_height_var = StringVar()
block_height_label = Label(frame_buttons, textvariable=bl_height_var)
block_height_label.grid(row=5, column=0, sticky=N + E, padx=15)

diff_msg_var = StringVar()
diff_msg_label = Label(frame_buttons, textvariable=diff_msg_var)
diff_msg_label.grid(row=6, column=0, sticky=N + E, padx=15)

sync_msg_var = StringVar()
sync_msg_label = Label(frame_buttons, textvariable=sync_msg_var)
sync_msg_label.grid(row=7, column=0, sticky=N + E, padx=15)

version_var = StringVar()
version_var_label = Label(frame_buttons, textvariable=version_var)
version_var_label.grid(row=8, column=0, sticky=N + E, padx=15)

hash_var = StringVar()
hash_var_label = Label(frame_buttons, textvariable=hash_var)
hash_var_label.grid(row=10, column=0, sticky=N + E, padx=15)

mempool_count_var = StringVar()
mempool_count_var_label = Label(frame_buttons, textvariable=mempool_count_var)
mempool_count_var_label.grid(row=11, column=0, sticky=N + E, padx=15)

current_var = StringVar()
current_var_label = Label(frame_buttons, textvariable=current_var)
current_var_label.grid(row=9, column=0, sticky=N + E, padx=15)

ann_var = StringVar()
ann_var_text = Text(frame_logo, width=20, height=4, font=("Tahoma", 8))
ann_var_text.grid(row=1, column=0, sticky=E + W, padx=5, pady=5)
ann_var_text.config(wrap=WORD)
ann_var_text.config(background="grey75")




encode_var = BooleanVar()
alias_cb_var = BooleanVar()
msg_var = BooleanVar()
encrypt_var = BooleanVar()
resolve_var = BooleanVar()
all_spend_var = BooleanVar()

# address and amount

# gui_address.configure(state="readonly")

gui_copy_address = Button(frame_entries, text="Copy", command=address_copy, font=("Tahoma", 7))
gui_copy_address.grid(row=0, column=2, sticky=W)

gui_paste_address = Button(frame_entries, text="Paste", command=address_insert, font=("Tahoma", 7))
gui_paste_address.grid(row=0, column=3, sticky=W)

gui_list_aliases = Button(frame_entries, text="Aliases", command=aliases_list, font=("Tahoma", 7))
gui_list_aliases.grid(row=0, column=4, sticky=W)

gui_watch = Button(frame_entries, text="Watch", command=watch, font=("Tahoma", 7))
gui_watch.grid(row=0, column=5, sticky=W)

gui_unwatch = Button(frame_entries, text="Unwatch", command=unwatch, font=("Tahoma", 7))
gui_unwatch.grid(row=0, column=6, sticky=W)

gui_copy_recipient = Button(frame_entries, text="Copy", command=recipient_copy, font=("Tahoma", 7))
gui_copy_recipient.grid(row=1, column=2, sticky=W)

gui_insert_recipient = Button(frame_entries, text="Paste", command=recipient_insert, font=("Tahoma", 7))
gui_insert_recipient.grid(row=1, column=3, sticky=W)

# gui_help = Button(frame_entries, text="Help", command=help, font=("Tahoma", 7))
# gui_help.grid(row=4, column=2, sticky=W + E, padx=(5, 0))

gui_all_spend = Checkbutton(frame_entries, text="All", variable=all_spend_var, command=all_spend, font=("Tahoma", 7))
gui_all_spend.grid(row=2, column=2, sticky=W)

gui_all_spend_clear = Button(frame_entries, text="Clear", command=all_spend_clear, font=("Tahoma", 7))
gui_all_spend_clear.grid(row=2, column=3, sticky=W)

data_insert_clipboard = Button(frame_entries, text="Paste", command=data_insert, font=("Tahoma", 7))
data_insert_clipboard.grid(row=3, column=2)

data_insert_clear = Button(frame_entries, text="Clear", command=data_insert_clear, font=("Tahoma", 7))
data_insert_clear.grid(row=3, column=3, sticky=W)

gui_copy_address = Button(frame_entries, text="Copy", command=url_copy, font=("Tahoma", 7))
gui_copy_address.grid(row=4, column=2, sticky=W)

url_insert_clipboard = Button(frame_entries, text="Paste", command=url_insert, font=("Tahoma", 7))
url_insert_clipboard.grid(row=4, column=3, sticky=W)

read_url_b = Button(frame_entries, text="Read", command=lambda: read_url_clicked(app_log, url.get()), font=("Tahoma", 7))
read_url_b.grid(row=4, column=4, sticky=W)

create_url_b = Button(frame_entries, text="Create", command=lambda: create_url_clicked(app_log, "pay", recipient.get(), amount.get(), openfield.get("1.0", END).strip()), font=("Tahoma", 7))
create_url_b.grid(row=4, column=5, sticky=W)




gui_address = Entry(frame_entries, width=60)
gui_address.grid(row=0, column=1, sticky=W)
gui_address.insert(0, myaddress)

recipient = Entry(frame_entries, width=60)
recipient.grid(row=1, column=1, sticky=W)

amount = Entry(frame_entries, width=60)
amount.grid(row=2, column=1, sticky=W)
amount.insert(0, "0.00000000")

openfield = Text(frame_entries, width=60, height=5, font=("Tahoma", 8))
openfield.grid(row=3, column=1, sticky=W)

url = Entry(frame_entries, width=60)
url.grid(row=4, column=1, sticky=W)
url.insert(0, "bis://")


encode = Checkbutton(frame_tick, text="Base64 Encoding", variable=encode_var, command=all_spend_check, width=14, anchor=W)
encode.grid(row=0, column=0, sticky=W)

msg = Checkbutton(frame_tick, text="Mark as Message", variable=msg_var, command=all_spend_check, width=14, anchor=W)
msg.grid(row=1, column=0, sticky=W)

encr = Checkbutton(frame_tick, text="Encrypt with PK", variable=encrypt_var, command=all_spend_check, width=14, anchor=W)
encr.grid(row=2, column=0, sticky=W)

resolve = Checkbutton(frame_tick, text="Resolve Aliases", variable=resolve_var, command=lambda: refresh(gui_address.get(), s), width=14, anchor=W)
resolve.grid(row=3, column=0, sticky=W)

alias_cb = Checkbutton(frame_tick, text="Alias Recipient", variable=alias_cb_var, command=None, width=14, anchor=W)
alias_cb.grid(row=4, column=0, sticky=W)

balance_enumerator = Entry(frame_entries, width=5)
# address and amount

# logo

#logo_hash_decoded = base64.b64decode(icons.logo_hash)
#logo = PhotoImage(data="graphics/logo.png")
logo = PhotoImage(file="graphics/logo.png")
Label(frame_logo, image=logo).grid(column=0, row=0)
# logo

node_connect()
refresh_auto()

root.mainloop()
