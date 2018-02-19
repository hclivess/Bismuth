import hashlib,z85

def checksum(string):
    #return base64.urlsafe_b64encode(string.encode("utf-8")).decode("utf-8")[:8]
    m = hashlib.md5()
    m.update(string.encode("utf-8"))
    return z85.encode(m.digest()).decode("utf-8")


def create_url(app_log, command, recipient, amount, openfield):
    if command == "pay":
        openfield_b64_encode = (z85.encode(openfield.encode("utf-8"))).decode("utf-8")
        url_partial = "bis://{}/{}/{}/{}/".format(command,recipient,amount,openfield_b64_encode)
        url_constructed = url_partial+checksum(url_partial)
        app_log.warning(url_constructed)        
        return url_constructed

def read_url(app_log, url):
    url_split = url.split("/")
    app_log.warning(url_split)
    reconstruct = "bis://{}/{}/{}/{}/".format(url_split[2],url_split[3],url_split[4],url_split[5],url_split[6])
    openfield_b64_decode = z85.decode(url_split[5]).decode("utf-8")

    if checksum(reconstruct) == url_split[6]:
        url_deconstructed = url_split[2],url_split[3],url_split[4],openfield_b64_decode
        print ("Checksum match")
        return url_deconstructed
    else:
        print ("Checksum mismatch",checksum(reconstruct),url_split[6])
        return


if __name__ == "__main__":
    #test
    import log
    app_log = log.log("node.log", "WARNING", "yes")

    print ("create_url", create_url (app_log, "pay", "recipient", "10", "eeeeeeeeeasdasdasdasdasdeeeeeeeeeeee"))
    print ("read_url", read_url(app_log, "bis://pay/recipient/10/wO2B:wO2B:wNPT<vrk%;B7wZ2wmoK&wO2B:wO2B:wO2B:/@JTuNo!+ZyM&O;AwwUzN"))