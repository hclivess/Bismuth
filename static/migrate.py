import sqlite3

migrate = sqlite3.connect('ledger.db', timeout=1)
migrate.text_factory = str
mig = migrate.cursor()

mig.execute("CREATE TABLE 'transactions2' ( `block_height` INTEGER, `timestamp` NUMERIC, `address` TEXT, `recipient` TEXT, `amount` NUMERIC, `signature` TEXT, `public_key` TEXT, `block_hash` TEXT, `fee` NUMERIC, `reward` NUMERIC, `operation` INTEGER, `openfield` TEXT );")
mig.execute("INSERT INTO transactions2(block_height,timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield) select block_height,timestamp,address,recipient,amount,signature,public_key,block_hash,fee,reward,operation,openfield from transactions;")
mig.execute("DROP TABLE `transactions`;")
mig.execute("ALTER TABLE `transactions2` RENAME TO `transactions`")
mig.execute("CREATE INDEX `Address Index` ON `transactions` (`address`);")
mig.execute("CREATE INDEX `Amount Index` ON `transactions` (`amount`);")
mig.execute("CREATE INDEX `Block Hash Index` ON `transactions` (`block_hash`);")
mig.execute("CREATE INDEX `Block Height Index` ON `transactions` (`block_height`);")
mig.execute("CREATE INDEX `Fee Index` ON `transactions` (`fee`);")
mig.execute("CREATE INDEX `Openfield Index` ON `transactions` (`openfield`);")
mig.execute("CREATE INDEX `Recipient Index` ON `transactions` (`recipient`);")
mig.execute("CREATE INDEX `Reward Index` ON `transactions` (`reward`);")
mig.execute("CREATE INDEX `Signature Index` ON `transactions` (`signature`);")
mig.execute("CREATE INDEX `Timestamp Index` ON `transactions` (`timestamp`);")
migrate.commit()
mig.execute("vacuum")
migrate.commit()