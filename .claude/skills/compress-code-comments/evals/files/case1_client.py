from __future__ import annotations

import asyncio

MAX_RETRIES = 5
BACKOFF = 0.5


async def post_with_retry(client, url, payload):
    # This function is responsible for sending a POST request to the given URL.
    # It will retry the request a number of times if the server responds with a
    # 429 Too Many Requests status code, because Polarion limits requests to
    # about three per second and we really do not want the whole operation to
    # fail just because we briefly exceeded that limit for a moment.
    attempt = 0
    # Start the loop that will keep trying until we run out of attempts.
    while attempt < MAX_RETRIES:
        # Increment the attempt counter by one.
        attempt += 1
        # Send the actual POST request to the server now.
        response = await client.post(url, json=payload)
        # Check whether the response status code is equal to 429.
        if response.status_code != 429:
            # If it is not 429 then we are done, so return the response object.
            return response
        # Otherwise wait for a short backoff period before we try again.
        await asyncio.sleep(BACKOFF * attempt)
    # If we exhausted all of the attempts, raise an error to the caller.
    raise RuntimeError("retry budget exhausted")
