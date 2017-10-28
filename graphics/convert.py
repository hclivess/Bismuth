import base64, json

with open("logo.gif", "rb") as imageFile:
    str = base64.b64encode(imageFile.read())
    print(len(str))

with open("logo.gif", "rb") as imageFile:

    str2 = json.dumps(list(imageFile.read()))
    print(len(str2))
    print(json.loads(str2))


#DEMO FOLLOWS
from tkinter import *
root = Tk()

# logo
frame = Frame(root, height=100, width=100)
frame.grid(row=0, column=1, sticky=W + E + N)

logo_hash_decoded = base64.b64decode(str)
logo = PhotoImage(data=logo_hash_decoded)

logo_hash_decoded2 = json.loads(str2)
logo2 = PhotoImage(data=logo_hash_decoded)

image = Label(frame, image=logo).grid(pady=25, padx=50, sticky=N)
image2 = Label(frame, image=logo2).grid(pady=25, padx=50, sticky=N)
# logo
root.mainloop()