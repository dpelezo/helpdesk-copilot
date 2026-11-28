import os

# Set before any app import: the API reads its keys at request time.
os.environ["API_KEYS"] = "test-key-1,test-key-2"
os.environ["TEMPORAL_HOST"] = "localhost:7233"
