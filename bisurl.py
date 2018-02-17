import hashlib,base64

def checksum(string):
    #return base64.urlsafe_b64encode(string.encode("utf-8")).decode("utf-8")[:8]
    m = hashlib.md5()
    m.update(string.encode("utf-8"))
    return m.hexdigest()[:8]


def create_url(command, address, recipient, amount, openfield):
    if command == "pay":
        openfield_b64_encode = (base64.urlsafe_b64encode(openfield.encode("utf-8"))).decode("utf-8")
        print (openfield_b64_encode)

        url_output = "bis://{}/{}/{}/{}/{}/".format(command,address,recipient,amount,openfield_b64_encode)
        return url_output+checksum(url_output)


def read_url(url):
    url_split = url.split("/")
    #print(url_split)


    reconstruct = "bis://{}/{}/{}/{}/{}/".format(url_split[2],url_split[3],url_split[4],url_split[5],url_split[6])
    openfield_b64_decode = base64.urlsafe_b64decode(url_split[6]).decode("utf-8")

    if checksum(reconstruct) == url_split[7]:
        print ("Checksum match")
        return url_split[2],url_split[3],url_split[4],url_split[5],openfield_b64_decode
    else:
        print ("Checksum mismatch",checksum(reconstruct),url_split[7])
        return

print ("create_url", create_url ("pay", "address", "recipient", "10", "eeeeeeeeeasdasdasdasdasdeeeeeeeeeeee"))
print ("read_url", read_url("bis://pay/address/recipient/10/ZWVlZWVlZWVlYXNkYXNkYXNkYXNkYXNkZWVlZWVlZWVlZWVl/7bfd1630"))