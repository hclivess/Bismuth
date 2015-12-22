from Tkinter import *

def send():
    print "Received tx command"

def node():
    print "Received node start command"

b = Button(text="Send transaction", command=send)
b.pack()

b2 = Button(text="Start node", command=node)
b2.pack()

mainloop()
