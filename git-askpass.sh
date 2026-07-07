#!/bin/sh
# GIT_ASKPASS helper: git invokes this for the password prompt only (the username is
# expected to already be in the clone URL as x-access-token@...). Echoing the token
# here keeps it out of .git/config entirely -- git never persists what an askpass
# script returns, unlike an embedded-in-URL token.
echo "$GITHUB_TOKEN"
