"""Django integration: add the PolarTrace middleware to settings.py.

Then run your project with the agent environment set:

    POLARTRACE_APP_NAME=example-django \
    POLARTRACE_LICENSE_KEY=<your-api-key> \
    polartrace-admin run-program python manage.py runserver
"""

MIDDLEWARE = [
    "polartrace.middleware.django_mw.DjangoPolarTraceMiddleware",
    # ... your other middleware
]
