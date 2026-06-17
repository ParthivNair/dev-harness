"""Ports: the interfaces (Protocols) the engine depends on.

The dependency arrow points inward — adapters implement these, the engine never
imports an adapter. Structural typing (``typing.Protocol``) means an adapter
satisfies a port just by shape; it never inherits from it.
"""
