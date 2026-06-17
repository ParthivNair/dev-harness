"""dev-harness: a headless, portable engine for orchestrating AI-assisted development.

See README.md for the architecture. The public seams are the ports in
``harness.ports``; the engine lives in ``harness.application``; concrete
implementations live in ``harness.adapters``; wiring happens in ``harness.cli``.
"""

__version__ = "0.1.0"
