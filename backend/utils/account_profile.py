"""
One reader for a field on an account's profile.

Account.profile_metadata holds two kinds of key: the named fields the platform's own loaders write
at the top level (contract_end, renewal_date, ...), and every other column a tenant uploads, which
utils.csv_ingest.row_attributes files under profile_metadata['attributes']. A vertical's config
names profile fields (an economics file's account_fields, a playbook's date trigger) without knowing
which kind they are, so anything that reads a config-named field goes through profile_field():
top level first, then attributes. Empty strings count as absent.
"""
from __future__ import annotations


def profile_field(account, name: str):
    pm = getattr(account, 'profile_metadata', None)
    if not isinstance(pm, dict) or not name:
        return None
    v = pm.get(name)
    if v in (None, ''):
        attrs = pm.get('attributes')
        v = attrs.get(name) if isinstance(attrs, dict) else None
    return None if v in (None, '') else v
