from Tkinter import *
window = Tk()

def send():
    print "Received tx command"
    to_address_input = to_address.get()
    print to_address_input
    amount_input = amount.get()
    print amount_input

def node():
    print "Received node start command"
    

def app_quit():
    print "Received quit command"
    window.destroy()

#address and amount
Label(window, text="To address", width=20).grid(row=0)
Label(window, text="Amount", width=20).grid(row=1)

to_address = Entry(window, width=60)
to_address.grid(row=0, column=1)

amount = Entry(window, width=60)
amount.grid(row=1, column=1)
#address and amount
    
#buttons
send_b = Button(window, text="Send transaction", command=send, height=1, width=15)
send_b.grid(row=3, column=0, sticky=W, pady=4)

start_b = Button(window, text="Start node", command=node, height=1, width=15)
start_b.grid(row=3, column=1, sticky=W, pady=4)

quit_b = Button(window, text="Quit", command=app_quit, height=1, width=15)
quit_b.grid(row=3, column=2, sticky=W, pady=4)
#buttons

mainloop()
