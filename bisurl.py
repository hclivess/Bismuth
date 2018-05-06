import hashlib, base64

def checksum(string):
    #return base64.urlsafe_b85encode(string.encode("utf-8")).decode("utf-8")[:8]
    m = hashlib.md5()
    m.update(string.encode("utf-8"))
    return base64.b85encode(m.digest()).decode("utf-8")


def create_url(app_log, command, recipient, amount, openfield):
    if command == "pay":
        openfield_b85_encode = (base64.b85encode(openfield.encode("utf-8"))).decode("utf-8")
        url_partial = "bis://{}/{}/{}/{}/".format(command,recipient,amount,openfield_b85_encode)
        url_constructed = url_partial+checksum(url_partial)
        app_log.warning(url_constructed)
        return url_constructed

def read_url(app_log, url):
    url_split = url.split("/")
    app_log.warning(url_split)
    reconstruct = "bis://{}/{}/{}/{}/".format(url_split[2],url_split[3],url_split[4],url_split[5],url_split[6])
    openfield_b85_decode = base64.b85decode(url_split[5]).decode("utf-8")

    if checksum(reconstruct) == url_split[6]:
        url_deconstructed = url_split[2],url_split[3],url_split[4],openfield_b85_decode
        app_log.warning("Checksum match")
        return url_deconstructed
    else:
        app_log.warning("Checksum mismatch",checksum(reconstruct),url_split[6])
        return


if __name__ == "__main__":
    #test
    import log
    app_log = log.log("node.log", "WARNING", True)

    print ("create_url", create_url (app_log, "pay", "recipient", "10", "dd611"))
    print ("read_url", read_url(app_log, "bis://pay/recipient/10/WMnomF#/`W4$&s&qu}j&z@EM@>rV"))