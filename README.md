# Kobeck – Readeck for Kobo

Kobo has a native client for Instapaper which can be used to connect to your own Readeck instance.

## Requirements

- Readeck version &ge; 0.20.0

## Configuration

Kobeck expects settings via environment variables.

- `READECK_URL`: The URL of your Readeck instance, e.g. `https://readeck.example.com`.
- `CONVERT_TO_JPEG`: Kobo actually seems to not support images except JPEGs. If this
  setting is set to `1`, then articles will have their images rewritten to use the
  **external** image service pocket-image-cache.com. Note that the service is operated
  by a third party for use with Pocket and can (probably will) be shutdown at any time.

## Readeck access token

Generate a Readeck API token and run the following script to encrypt the Readeck token for the Kobo settings file:

```sh
./bin/generate-access-token "$READECK_TOKEN" "$KOBO_SERIAL"
```

The output of the script is your access token for the Kobo settings file.

## Kobo settings

In `Kobo eReader.conf` change or add the following settings:

```
[OneStoreServices]
api_endpoint=https://kobeck.example.com/storeapi
instapaper_env_url=https://kobeck.example.com/instapaper

[Instapaper]
AccessToken=@ByteArray(<GENERATED-ACCESS-TOKEN>)
```

Replace `kobeck.example.com` with the hostname of your Kobeck instance.

Proxying the API endpoint is necessary because otherwise `instapaper_env_url` will be reset on every sync.

## nginx

See `conf/nginx.conf` for an example configuration.
