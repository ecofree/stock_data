"""Compatibility shim - implementation lives in collectors/collect_dingpan.py."""
import collectors.collect_dingpan as _impl

globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})
del _impl
