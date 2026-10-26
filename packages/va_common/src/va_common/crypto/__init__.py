"""Envelope encryption for tenant and platform secrets (specs/28-security.md §3)."""

from va_common.crypto.blob import (
    BLOB_VERSION,
    DATA_KEY_SIZE,
    NONCE_SIZE,
    TAG_SIZE,
    CryptoError,
    DecryptionError,
    EncryptedBlob,
    column_aad,
)
from va_common.crypto.cache import DataKeyCache
from va_common.crypto.envelope import DataKeyStore, Envelope
from va_common.crypto.kms import (
    LOCAL_KEK_ID,
    NIL_ORG_ID,
    AwsKmsProvider,
    DataKey,
    KmsClient,
    KmsProvider,
    LocalKmsProvider,
    WrappedDataKey,
    aws_kms_provider,
    open_kms_provider,
)

__all__ = [
    "BLOB_VERSION",
    "DATA_KEY_SIZE",
    "LOCAL_KEK_ID",
    "NIL_ORG_ID",
    "NONCE_SIZE",
    "TAG_SIZE",
    "AwsKmsProvider",
    "CryptoError",
    "DataKey",
    "DataKeyCache",
    "DataKeyStore",
    "DecryptionError",
    "EncryptedBlob",
    "Envelope",
    "KmsClient",
    "KmsProvider",
    "LocalKmsProvider",
    "WrappedDataKey",
    "aws_kms_provider",
    "column_aad",
    "open_kms_provider",
]
