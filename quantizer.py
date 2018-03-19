from decimal import *

def quantize_eight(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.00000000'))
    #value = '{:.8f}'.format(value)
    return value

def quantize_ten(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.0000000000'))
    return value