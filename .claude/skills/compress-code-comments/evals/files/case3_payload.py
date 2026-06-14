from __future__ import annotations


def normalize(attrs):
    result = {}  # create a new empty dictionary called result
    for key, val in attrs.items():  # iterate over each of the attribute items
        # Polarion treats a None value as an instruction to clear that field
        # back to its default, so we deliberately drop None entries here to
        # avoid silently wiping fields that we never intended to change at all.
        if val is None:
            continue  # skip it and move on to the next one
        result[key] = val  # store the value in the result under its key
    # todo: maybe we should also strip out empty strings here someday, not sure
    return result  # return the final result dictionary to the caller
