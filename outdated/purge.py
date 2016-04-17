                    #purge nodes start
                    s_purge = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        peer_list = []

                        print "Checking peers"
                        s_purge.connect((HOST, PORT))#save a new peer file with only active nodes
                        s_purge.close()

                        peer_list.append("('"+str(HOST)+"', '"+str(PORT)+"')")

                        open("peers.txt", 'w').close() #purge file completely
                        peer_list_file = open("peers.txt", 'a')
                        for x in peer_list:
                            peer_list_file.write(x+"\n")
                            print x+" kept" #append peers to which connection is possible
                        peer_list_file.close()
                    except:
                        print "Could not connect to "+str(HOST)+":"+str(PORT)+", purged"
                        #raise #for testing purposes only
                        break
                    #purge nodes end
