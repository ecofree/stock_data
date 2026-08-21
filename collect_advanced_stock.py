"""Compatibility shim - implementation lives in collectors/collect_advanced_stock.py."""
import collectors.collect_advanced_stock as _impl

globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})
del _impl
