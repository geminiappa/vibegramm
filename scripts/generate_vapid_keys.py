#!/usr/bin/env python3
"""Generate VAPID keys for Vibegram Web Push. Keep VAPID_PRIVATE_KEY secret."""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

private = ec.generate_private_key(ec.SECP256R1())
private_scalar = private.private_numbers().private_value.to_bytes(32, "big")
public_point = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
print("VAPID_PUBLIC_KEY=" + b64url(public_point))
print("VAPID_PRIVATE_KEY=" + b64url(private_scalar))
print("VAPID_SUBJECT=mailto:admin@vibegramm.onrender.com")
print("\nAdd these values to Render → Environment. Never expose VAPID_PRIVATE_KEY in client code or GitHub.")
