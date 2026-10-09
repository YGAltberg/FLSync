"""Logging for the FLSync modules. Goes to FL's Script output; tests swap the sink."""

sink = print


def log(msg):
    sink("[FLSync] " + msg)
