"""Compatibility shim - implementation lives in collectors/collect_ladder.py."""
import collectors.collect_ladder as _impl

globals().update({k: v for k, v in vars(_impl).items() if not k.startswith('__')})
del _impl
