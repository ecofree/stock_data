"""Compatibility shim - implementation lives in collectors/collect_market.py."""
import collectors.collect_market as _impl

globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})
del _impl
