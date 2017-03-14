import base64

with open("b2.gif", "rb") as imageFile:
    str = base64.b64encode(imageFile.read())
    print str
