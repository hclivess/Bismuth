from Tkinter import *

def callback():
    print "clicked!"

def callback2():
    print "clicked too!"

b = Button(text="click me", command=callback)
b2 = Button(text="click me too", command=callback2)
b.pack()
b2.pack()

mainloop()
