import base64

with open("logo_new_80.gif", "rb") as imageFile:
    str = base64.b64encode(imageFile.read())
    print str
