"""
One-time YouTube OAuth consent.

Opens the Google consent screen in a browser and saves the resulting token to
yt_token.pickle, after which the benchmarks and the app can upload without
further interaction.

You have to complete the sign-in yourself -- the consent screen asks for your
Google account password, which no automated agent should ever be handed.

Usage: python bench/yt_auth.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from youtube_api import (CLIENT_SECRETS_FILE, TOKEN_FILE,
                         get_youtube_service)


def main():
    if not os.path.exists(CLIENT_SECRETS_FILE):
        print(f'No client_secrets.json at {CLIENT_SECRETS_FILE}')
        return 1

    if os.path.exists(TOKEN_FILE):
        print(f'Token already present at {TOKEN_FILE}')
    else:
        print('A browser window will open for Google sign-in.')
        print('Approve the "Manage your YouTube videos" scope to continue.\n')

    try:
        # Deliberately does not call channels().list to confirm: that needs a
        # youtube.readonly scope this app has no other use for, and asking for
        # a broader grant just to print a channel name is the wrong trade.
        get_youtube_service()
        print(f'\nAuthorised. Token saved to {TOKEN_FILE}')
        print('Scope granted: youtube.upload (upload only, no read access).')
        return 0
    except Exception as e:
        print(f'\nAuthorisation failed: {type(e).__name__}: {e}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
