"""The cloud side of AIworker v2: the scheduled pipeline that runs on GitHub
Actions against Supabase, with no machine of the owner's switched on.

The database enforces the safety rules (who may approve, which status moves
are legal, approval invalidated by edits); this package only does the work
and asks the database for permission through the same functions the phone
uses.
"""
