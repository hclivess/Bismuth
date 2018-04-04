from decimal import *

def quantize_two(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.00'))
    return value

def quantize_eight(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.00000000'))
    return value

def quantize_ten(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.0000000000'))
    return value