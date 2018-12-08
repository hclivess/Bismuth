def verify(h3):
    try:
        app_log.warning("Blockchain verification started...")
        # verify blockchain
        execute(h3, ("SELECT Count(*) FROM transactions"))
        db_rows = h3.fetchone()[0]
        app_log.warning("Total steps: {}".format(db_rows))

        # verify genesis
        if full_ledger:
            execute(h3, ("SELECT block_height, recipient FROM transactions WHERE block_height = 1"))
            result = h3.fetchall()[0]
            block_height = result[0]
            genesis = result[1]
            app_log.warning("Genesis: {}".format(genesis))
            if str(genesis) != genesis_conf and int(
                    block_height) == 0:  # change this line to your genesis address if you want to clone
                app_log.warning("Invalid genesis address")
                sys.exit(1)
        # verify genesis

        db_hashes = {
            '27258-1493755375.23': 'acd6044591c5baf121e581225724fc13400941c7',
            '27298-1493755830.58': '481ec856b50a5ae4f5b96de60a8eda75eccd2163',
            '30440-1493768123.08': 'ed11b24530dbcc866ce9be773bfad14967a0e3eb',
            '32127-1493775151.92': 'e594d04ad9e554bce63593b81f9444056dd1705d',
            '32128-1493775170.17': '07a8c49d00e703f1e9518c7d6fa11d918d5a9036',
            '37732-1493799037.60': '43c064309eff3b3f065414d7752f23e1de1e70cd',
            '37898-1493799317.40': '2e85b5c4513f5e8f3c83a480aea02d9787496b7a',
            '37898-1493799774.46': '4ea899b3bdd943a9f164265d51b9427f1316ce39',
            '38083-1493800650.67': '65e93aab149c7e77e383e0f9eb1e7f9a021732a0',
            '52233-1493876901.73': '29653fdefc6ca98aadeab37884383fedf9e031b3',
            '52239-1493876963.71': '4c0e262de64a5e792601937a333ca2bf6d6681f2',
            '52282-1493877169.29': '808f90534e7ba68ee60bb2ea4530f5ff7b9d8dea',
            '52308-1493877257.85': '8919548fdbc5093a6e9320818a0ca058449e29c2',
            '52393-1493877463.97': '0eba7623a44441d2535eafea4655e8ef524f3719',
            '62507-1493946372.50': '81c9ca175d09f47497a57efeb51d16ee78ddc232',
            '70094-1494032933.14': '2ca4403387e84b95ed558e7c9350c43efff8225c'
        }
        invalid = 0
        for row in execute(h3,
                           ('SELECT * FROM transactions WHERE block_height > 1 and reward = 0 ORDER BY block_height')):

            db_block_height = str(row[0])
            db_timestamp = '%.2f' % (quantize_two(row[1]))
            db_address = str(row[2])[:56]
            db_recipient = str(row[3])[:56]
            db_amount = '%.8f' % (quantize_eight(row[4]))
            db_signature_enc = str(row[5])[:684]
            db_public_key_hashed = str(row[6])[:1068]
            db_public_key = RSA.importKey(base64.b64decode(db_public_key_hashed))
            db_operation = str(row[10])[:30]
            db_openfield = str(row[11])  # no limit for backward compatibility

            db_transaction = (db_timestamp, db_address, db_recipient, db_amount, db_operation, db_openfield)

            db_signature_dec = base64.b64decode(db_signature_enc)
            verifier = PKCS1_v1_5.new(db_public_key)
            hash = SHA.new(str(db_transaction).encode("utf-8"))
            if verifier.verify(hash, db_signature_dec):
                pass
            else:
                try:
                    if hash.hexdigest() != db_hashes[db_block_height + "-" + db_timestamp]:
                        app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                        invalid = invalid + 1
                except:
                    app_log.warning("Signature validation problem: {} {}".format(db_block_height, db_transaction))
                    invalid = invalid + 1

        if invalid == 0:
            app_log.warning("All transacitons in the local ledger are valid")

    except Exception as e:
        app_log.warning("Error: {}".format(e))
        raise
