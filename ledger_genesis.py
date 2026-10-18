"""The canonical genesis block (height 1), shared by mainnet, testnet and regnet.

A fresh ledger is seeded with this one block; everything above it is synced from peers (or mined, on
regnet). ``rows()`` returns it in the stored 12-field form for the current amount storage mode.

``MAINNET_ALLOCATIONS`` are fixed balance credits the mainnet ledger carries without any block row: the
concluded Development Reward and Hypernode Payouts totals. Until block 4,380,000 those rewards were minted
locally every 10th block as synthetic ledger rows; the rows are gone, their totals stay as allocations (in
integer units), so both addresses' balances equal the network's. Every block they apply below is anchored
by the block-hash checkpoints (validation_exceptions), the last of which is above 4,380,000.
"""
import amounts

GENESIS_HEIGHT = 1
GENESIS_TIMESTAMP = "1493640955.47"
GENESIS_ADDRESS = "4edadac9093d9326ee4b17f869b14f1a2534f96f9c5d7b48dc9acaed"
GENESIS_BLOCK_HASH = "7a0f384876aca3871adbde8622a87f8b971ede0ed8ee10425e3958a1"
GENESIS_SIGNATURE = "DKiWVr+GQHrsEUlu3qEQnsB5rznU4Is7RFLnPmHM1grobiUFHup0kSWiN83gBkNS9LgE57RXUEJvxMKc+9hIAzYE8EwGtO3RsXxkqPTT1v19CguN0iqE4nIM8Bur53/Djs5a1bH/R8EMersemZY1bDJ4jTeeba6yqFxmevGk/gw="
GENESIS_PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nMIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDKvLTbDx85a1ugb/6xMMhVOq6U\n2GeYT8+Iq2z9FwIMR40l2ttGqNK7varNccFLIu8Kn4ogDQs3WSWQCxNkhZh/FqzF\nYYa3/ItPPfzrXqgajwD8q4Zt4Ymjt8+2BkImPjjFNkuTQIz2Iu3yFqOIxLdjMw7n\nUVu9tFPiUkD0VnDPLQIDAQAB\n-----END PUBLIC KEY-----"


def rows():
    reward = str(amounts.to_units(1)) if amounts.LEDGER_INTEGER else "1"
    return [(str(GENESIS_HEIGHT), GENESIS_TIMESTAMP, "genesis", GENESIS_ADDRESS, "0", GENESIS_SIGNATURE,
             GENESIS_PUBLIC_KEY, GENESIS_BLOCK_HASH, "0", reward, "1", "genesis")]


# address -> integer units (1 BIS = 10**8)
MAINNET_ALLOCATIONS = {
    GENESIS_ADDRESS: 315147652720086,                                   # Development Reward  3151476.52720086
    "3e08b5538a4509d9daa99e01ca5912cda3e98a7f79ca01248c2bde16": 652116733333333,   # Hypernode Payouts  6521167.33333333
}


def allocations(node):
    return dict(MAINNET_ALLOCATIONS) if getattr(node, "is_mainnet", False) else {}
