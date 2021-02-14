from kubernetes import client, config, watch
from kubernetes.client.rest import ApiException
from urllib3.exceptions import ProtocolError

import argparse
import logging
import os
import time
import sentry_sdk


SDK_VALUE = {"name": "sentry-kubernetes", "version": "2.0.0"}

# mapping from k8s event types to event levels
LEVEL_MAPPING = {"normal": "info"}

LOG_LEVEL = os.environ.get("LOG_LEVEL", "error")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")


def _listify_env(name, default=None):
    value = os.getenv(name) or ""
    result = []

    for item in value.split(","):
        item = item.strip()
        if item:
            result.append(item)

    if not result and default:
        return default

    return result


MANGLE_NAMES = _listify_env("MANGLE_NAMES")
EVENT_LEVELS = _listify_env("EVENT_LEVELS", ["warning", "error"])
REASONS_EXCLUDED = _listify_env("REASON_FILTER")
COMPONENTS_EXCLUDED = _listify_env("COMPONENT_FILTER")
DEPRECATED_EVENT_NAMESPACE = (
    [os.getenv("EVENT_NAMESPACE")] if os.getenv("EVENT_NAMESPACE") else None
)
EVENT_NAMESPACES = _listify_env("EVENT_NAMESPACES", DEPRECATED_EVENT_NAMESPACE)
EVENT_NAMESPACES_EXCLUDED = _listify_env("EVENT_NAMESPACES_EXCLUDED")


def watch_loop():
    logging.info("Starting Kubernetes watcher")
    v1 = client.CoreV1Api()
    w = watch.Watch()
    sentry_sdk.init(
        debug=True if logging.root.level == logging.DEBUG else False,
    )

    if EVENT_NAMESPACES and len(EVENT_NAMESPACES) == 1:
        stream = w.stream(v1.list_namespaced_event, EVENT_NAMESPACES[0])
    else:
        stream = w.stream(v1.list_event_for_all_namespaces)

    for event in stream:
        logging.debug("event: %s" % event)

        event_type = event["type"].lower()
        event = event["object"]

        meta = {k: v for k, v in event.metadata.to_dict().items() if v is not None}

        level = event.type and event.type.lower()
        level = LEVEL_MAPPING.get(level, level)

        component = reason = namespace = name = short_name = kind = None
        if event.source:
            source = event.source.to_dict()

            if "component" in source:
                component = source["component"]
                if COMPONENTS_EXCLUDED and component in COMPONENTS_EXCLUDED:
                    continue

        if event.reason:
            reason = event.reason
            if REASONS_EXCLUDED and reason in REASONS_EXCLUDED:
                continue

        if event.involved_object and event.involved_object.namespace:
            namespace = event.involved_object.namespace
        elif "namespace" in meta:
            namespace = meta["namespace"]

        if namespace and EVENT_NAMESPACES and namespace not in EVENT_NAMESPACES:
            continue

        if (
            namespace
            and EVENT_NAMESPACES_EXCLUDED
            and namespace in EVENT_NAMESPACES_EXCLUDED
        ):
            continue

        if event.involved_object and event.involved_object.kind:
            kind = event.involved_object.kind

        if event.involved_object and event.involved_object.name:
            name = event.involved_object.name
            if not MANGLE_NAMES or kind in MANGLE_NAMES:
                bits = name.split("-")
                if len(bits) in (1, 2):
                    short_name = bits[0]
                else:
                    short_name = "-".join(bits[:-2])
            else:
                short_name = name

        message = event.message

        if namespace and short_name:
            obj_name = "(%s/%s)" % (namespace, short_name)
        else:
            obj_name = "(%s)" % (namespace,)

        if level in EVENT_LEVELS or event_type in ("error",):
            if event.involved_object:
                meta["involved_object"] = {
                    k: v
                    for k, v in event.involved_object.to_dict().items()
                    if v is not None
                }

            if CLUSTER_NAME:
                sentry_sdk.set_tag("cluster", CLUSTER_NAME)

            if component:
                sentry_sdk.set_tag("component", component)

            if reason:
                sentry_sdk.set_tag("reason", event.reason)

            if namespace:
                sentry_sdk.set_tag("namespace", namespace)

            if short_name:
                sentry_sdk.set_tag("short_name", short_name)

            if kind:
                sentry_sdk.set_tag("kind", kind)

            if obj_name:
                sentry_sdk.set_tag("culprit", obj_name)

            sentry_sdk.set_context("sentry-kubernetes SDK", SDK_VALUE)

            with sentry_sdk.push_scope() as scope:
                scope.fingerprint = [event.reason, short_name, kind, namespace]
                sentry_sdk.capture_message(message, level)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-level", default=LOG_LEVEL)
    args = parser.parse_args()

    log_level = args.log_level.upper()
    logging.basicConfig(format="%(asctime)s %(message)s", level=log_level)
    logging.debug("log_level: %s" % log_level)

    try:
        config.load_incluster_config()
    except:  # noqa: E722
        config.load_kube_config()

    while True:
        try:
            watch_loop()
        except ApiException as e:
            logging.error(
                "Exception when calling CoreV1Api->list_event_for_all_namespaces: %s\n"
                % e
            )
            time.sleep(5)
        except ProtocolError:
            logging.warning("ProtocolError exception. Continuing...")
