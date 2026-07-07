# Gumloop Fork of the Official Celery SDK

This fork was created for use with gumloop since the official package does not have spawn support for concurrency.

The core should be rebased from `main` of the official repository from time to time.

### Official Repository

[Official Repository URL](https://github.com/celery/celery)

## Building and Deploying

### Building the Package

To build the package:

```bash
python -m build
```

This will create distribution packages in the `dist/` directory.

### Deploying to Artifact Registry

To deploy to Google Artifact Registry:

```bash
python -m twine upload --repository-url https://us-west1-python.pkg.dev/agenthub-dev/gumloop/ dist/* --skip-existing
```

You'll need appropriate authentication credentials configured for the Artifact Registry repository.
