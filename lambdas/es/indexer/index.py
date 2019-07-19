"""
phone data into elastic for supported file extensions.
note: we truncated inbound documents to no more than DOC_SIZE_LIMIT characters
(this bounds memory pressure and request size to elastic)
"""

from datetime import datetime
from math import floor
import json
import os
from urllib.parse import unquote, unquote_plus

from aws_requests_auth.aws_auth import AWSRequestsAuth
import boto3
from elasticsearch import Elasticsearch, RequestsHttpConnection
from elasticsearch.helpers import bulk
import nbformat
from tenacity import stop_after_attempt, stop_after_delay, retry, wait_exponential

CONTENT_INDEX_EXTS = [
    ".csv",
    ".html",
    ".ipynb",
    ".json",
    ".md",
    ".rmd",
    ".txt",
    ".xml"
]
# 10 MB, see https://amzn.to/2xJpngN
CHUNK_LIMIT_BYTES = 10_000_000
DOC_SIZE_LIMIT_BYTES = 10_000
ELASTIC_TIMEOUT = 20
MAX_RETRY = 10 # prevent long-running lambdas due to malformed calls
NB_VERSION = 4 # default notebook version for nbformat
# signifies that the object is truly deleted, not to be confused with
# s3:ObjectRemoved:DeleteMarkerCreated, which we may see in versioned buckets
# see https://docs.aws.amazon.com/AmazonS3/latest/dev/NotificationHowTo.html
OBJECT_DELETE = "ObjectRemoved:Delete"
QUEUE_LIMIT_BYTES = 100_000_000# 100MB
RETRY_429 = 5
TEST_EVENT = "s3:TestEvent"
S3_CLIENT = boto3.client("s3")

class DocumentQueue:
    """transient in-memory queue for documents to be indexed"""
    def __init__(self, context, retry_errors=True):
        """constructor"""
        self.queue = []
        self.retry_errors = retry_errors
        self.size = 0
        self.context = context

    def append(
            self,
            event_type,
            size=0,
            meta=None,
            *,
            last_modified,
            bucket,
            ext,
            key,
            text,
            etag,
            version_id
    ):
        """format event as document and queue it up"""
        if text:
            # documents will dominate memory footprint, there is also a fixed
            # size for the rest of the doc that we do not account for
            self.size += min(size, DOC_SIZE_LIMIT_BYTES)
        # On types and fields, see
        # https://www.elastic.co/guide/en/elasticsearch/reference/master/mapping.html
        body = {
            # Elastic native keys
            # : is a legal character for S3 keys, so look for its last occurrence
            # if you want to find the, potentially empty, version_id
            "_id": f"{key}:{version_id}",
            "_index": bucket,
            # index will upsert (and clobber existing equivalent _ids)
            "_op_type": "delete" if event_type == OBJECT_DELETE else "index",
            "_type": "_doc",
            # Quilt keys
            # Be VERY CAREFUL changing these values as a type change can cause a
            # mapper_parsing_exception that below code won't handle
            "etag": etag,
            "ext": ext,
            "event": event_type,
            "size": size,
            "text": text,
            "key": key,
            "last_modified": last_modified.isoformat(),
            "updated": datetime.utcnow().isoformat(),
            "version_id": version_id
        }

        body = {**body, **transform_meta(meta or {})}

        body["meta_text"] = " ".join([body["meta_text"], key])

        self.append_document(body)

        if self.size > QUEUE_LIMIT_BYTES:
            self.send_all()

    def append_document(self, doc):
        """append well-formed documents (used for retry or by append())"""
        self.queue.append(doc)

    def is_empty(self):
        """is the queue empty?"""
        return len(self.queue) == 0

    def send_all(self):
        """flush self.queue in a bulk call"""
        if self.is_empty():
            return
        elastic_host = os.environ["ES_HOST"]
        try:
            awsauth = AWSRequestsAuth(
                # These environment variables are automatically set by Lambda
                aws_access_key=os.environ["AWS_ACCESS_KEY_ID"],
                aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
                aws_token=os.environ["AWS_SESSION_TOKEN"],
                aws_host=elastic_host,
                aws_region=boto3.session.Session().region_name,
                aws_service="es"
            )

            time_remaining = get_time_remaining(self.context)

            elastic = Elasticsearch(
                hosts=[{"host": elastic_host, "port": 443}],
                http_auth=awsauth,
                max_backoff=time_remaining,
                # Give ES time to repsond when under laod
                timeout=ELASTIC_TIMEOUT,
                use_ssl=True,
                verify_certs=True,
                connection_class=RequestsHttpConnection
            )

            _, errors = bulk(
                elastic,
                iter(self.queue),
                # Some magic numbers to reduce memory pressure
                # e.g. see https://github.com/wagtail/wagtail/issues/4554
                # The stated default is max_chunk_bytes=10485760, but with default
                # ES will still return an exception stating that the very
                # same request size limit has been exceeded
                chunk_size=100,
                max_chunk_bytes=CHUNK_LIMIT_BYTES,
                # number of retries for 429 (too many requests only)
                # all other errors handled by our code
                max_retries=RETRY_429,
                # we'll process errors on our own
                raise_on_error=False,
                raise_on_exception=False
            )
            # reset the size count
            self.size = 0
            # Retry only if this is a first-generation queue
            # (prevents infinite regress on failing documents)
            if self.retry_errors:
                # this is a second genration queue, so don't let it retry
                # anything other than 429s
                error_queue = DocumentQueue(self.context, retry_errors=False)
                for error in errors:
                    print(error)
                    # can be dict or string *sigh*
                    inner = error.get("index", {})
                    error_info = inner.get("error")
                    doc_id = inner.get("_id")

                    if isinstance(error_info, dict):
                        error_type = error_info.get("type", "")
                        if 'mapper_parsing_exception' in error_type:
                            replay = next(doc for doc in self.queue if doc["_id"] == doc_id)
                            replay['user_meta'] = replay['system'] = {}
                            error_queue.append_document(replay)
                # recursive but never goes more than one level deep
                error_queue.send_all()

        except Exception as ex:# pylint: disable=broad-except
            print("Fatal, unexpected Exception in send_all", ex)
            import traceback
            traceback.print_tb(ex.__traceback__)

def get_contents(context, bucket, key, ext, *, etag, version_id, size):
    """get the byte contents of a file"""
    content = ""
    if ext in CONTENT_INDEX_EXTS:
        # we treat notebooks separately because we need to parse them in
        # this lambda, which means we need the whole object
        if ext == ".ipynb":
            # Ginormous notebooks could still cause a problem here
            content = get_notebook_cells(
                context,
                bucket,
                key,
                size,
                etag=etag,
                version_id=version_id
            )
            content = trim_to_bytes(content)
        else:
            content = get_plain_text(
                context,
                bucket,
                key,
                size,
                etag=etag,
                version_id=version_id
            )

    return content

def extract_text(notebook_str):
    """ Extract code and markdown
    Args:
        * nb - notebook as a string
    Returns:
        * str - select code and markdown source (and outputs)
    Pre:
        * notebook is well-formed per notebook version 4
        * "cell_type" is defined for all cells
        * "source" defined for all "code" and "markdown" cells
    Throws:
        * Anything nbformat.reads() can throw :( which is diverse and poorly
        documented, hence the `except Exception` in handler()
    Notes:
        * Deliberately decided not to index output streams and display strings
        because they were noisy and low value
        * Tested this code against ~6400 Jupyter notebooks in
        s3://alpha-quilt-storage/tree/notebook-search/
        * Might be useful to index "cell_type" : "raw" in the future
    See also:
        * Format reference https://nbformat.readthedocs.io/en/latest/format_description.html
    """
    formatted = nbformat.reads(notebook_str, as_version=NB_VERSION)
    text = []
    for cell in formatted.get("cells", []):
        if "source" in cell and "cell_type" in cell:
            if cell["cell_type"] == "code" or cell["cell_type"] == "markdown":
                text.append(cell["source"])

    return "\n".join(text)

def get_notebook_cells(context, bucket, key, size, *, etag, version_id):
    """extract cells for ipynb notebooks for indexing"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            context,
            bucket,
            key,
            size,
            etag=etag,
            version_id=version_id
        )
        notebook = obj["Body"].read().decode("utf-8")
        text = extract_text(notebook)
    except UnicodeDecodeError as uni:
        print(f"Unicode decode error in {key}: {uni}")
    except (json.JSONDecodeError, nbformat.reader.NotJSONError):
        print(f"Invalid JSON in {key}.")
    except (KeyError, AttributeError)  as err:
        print(f"Missing key in {key}: {err}")
    # there might be more errors than covered by test_read_notebook
    # better not to fail altogether
    except Exception as exc:#pylint: disable=broad-except
        print(f"Exception in file {key}: {exc}")

    return text

def get_plain_text(context, bucket, key, size, *, etag, version_id):
    """get plain text object contents"""
    text = ""
    try:
        obj = retry_s3(
            "get",
            context,
            bucket,
            key,
            size,
            etag=etag,
            version_id=version_id
        )
        text = obj["Body"].read().decode("utf-8")
    except UnicodeDecodeError as ex:
        print(f"Unicode decode error in {key}", ex)

    return text

def get_time_remaining(context):
    """returns time remaining in seconds before lambda context is shut down"""
    time_remaining = floor(context.get_remaining_time_in_millis()/1000)
    if time_remaining < 30:
        print(
            f"Warning: Lambda function has less than {time_remaining} seconds."
            " Consider reducing bulk batch size."
        )

    return time_remaining

def transform_meta(meta):
    """ Reshapes metadata for indexing in ES """
    helium = meta.get("helium")
    user_meta = {}
    comment = ""
    target = ""

    if helium:
        user_meta = helium.pop("user_meta", {})
        comment = helium.pop("comment", "") or ""
        target = helium.pop("target", "") or ""

    meta_text_parts = [comment, target]

    if helium:
        meta_text_parts.append(json.dumps(helium))
    if user_meta:
        meta_text_parts.append(json.dumps(user_meta))

    return {
        "system_meta": helium,
        "user_meta": user_meta,
        "comment": comment,
        "target": target,
        "meta_text": " ".join(meta_text_parts)
    }

def handler(event, context):
    """enumerate S3 keys in event, extract relevant data and metadata,
    queue events, send to elastic via bulk() API
    """
    try:
        # message is a proper SQS message, which either contains a single event
        # (from the bucket notification system) or batch-many events as determined
        # by enterprise/**/bulk_loader.py
        for message in event["Records"]:
            body = json.loads(message["body"])
            body_message = json.loads(body["Message"])
            if "Records" not in body_message:
                if body_message.get("Event") == TEST_EVENT:
                    # Consume and ignore this event, which is an initial message from
                    # SQS; see https://forums.aws.amazon.com/thread.jspa?threadID=84331
                    continue
                else:
                    print("Unexpected message['body']. No 'Records' key.", message)
            batch_processor = DocumentQueue(context)
            events = body_message.get("Records", [])
            # event is a single S3 event
            for event_ in events:
                try:
                    event_name = event_["eventName"]
                    bucket = unquote(event_["s3"]["bucket"]["name"])
                    # In the grand tradition of IE6, S3 events turn spaces into '+'
                    key = unquote_plus(event_["s3"]["object"]["key"])
                    version_id = event_["s3"]["object"].get("versionId")
                    version_id = unquote(version_id) if version_id else None
                    etag = unquote(event_["s3"]["object"]["eTag"])
                    _, ext = os.path.splitext(key)
                    ext = ext.lower()

                    head = retry_s3(
                        "head",
                        context,
                        bucket,
                        key,
                        size,
                        version_id=version_id,
                        etag=etag
                    )

                    size = head["ContentLength"]
                    last_modified = head["LastModified"]
                    meta = head["Metadata"]
                    text = ""

                    if event_name == OBJECT_DELETE:
                        batch_processor.append(
                            event_name,
                            bucket=bucket,
                            ext=ext,
                            etag=etag,
                            key=key,
                            last_modified=last_modified,
                            text=text,
                            version_id=version_id
                        )
                        continue

                    _, ext = os.path.splitext(key)
                    ext = ext.lower()
                    text = get_contents(
                        context,
                        bucket,
                        key,
                        ext,
                        etag=etag,
                        version_id=version_id,
                        size=size
                    )
                    # decode Quilt-specific metadata
                    try:
                        if "helium" in meta:
                            meta["helium"] = json.loads(meta["helium"])
                    except (KeyError, json.JSONDecodeError):
                        print("Unable to parse Quilt 'helium' metadata", meta)

                    batch_processor.append(
                        event_name,
                        bucket=bucket,
                        key=key,
                        ext=ext,
                        meta=meta,
                        etag=etag,
                        version_id=version_id,
                        last_modified=last_modified,
                        size=size,
                        text=text
                    )
                except Exception as exc:# pylint: disable=broad-except
                    print("Fatal exception for record", event_, exc)
                    import traceback
                    traceback.print_tb(exc.__traceback__)
            # flush the queue
            batch_processor.send_all()

    except Exception as exc:# pylint: disable=broad-except
        print("Fatal exception for message", message, event_, exc)
        import traceback
        traceback.print_tb(exc.__traceback__)
        # Fail the lambda so the message is not dequeued
        raise exc

def retry_s3(
        operation,
        context,
        bucket,
        key,
        size,
        limit=DOC_SIZE_LIMIT_BYTES,
        *,
        etag,
        version_id
):
    """retry head or get operation to S3 with; stop before we run out of time.
    retry is necessary since, due to eventual consistency, we may not
    always get the required version of the object.
    """
    if operation not in ["get", "head"]:
        raise ValueError(f"unexpected operation: {operation}")
    if operation == "head":
        function_ = S3_CLIENT.head_object
    else:
        function_ = S3_CLIENT.get_object

    time_remaining = get_time_remaining(context)
    # use a local function so that we can parameterize to time_remaining
    @retry(
        # debug
        stop=(stop_after_delay(time_remaining) | stop_after_attempt(MAX_RETRY)),
        wait=wait_exponential(multiplier=2, min=4, max=30)
    )
    def call():
        # we can't use Range= if size == 0 because byte 0 doesn't exist
        # and we'll get an exception
        if size == 0:
            if version_id:
                return function_(
                    Bucket=bucket,
                    Key=key,
                    VersionId=version_id
                )
            # else
            return function_(
                Bucket=bucket,
                Key=key,
                IfMatch=etag
            )

        if version_id:
            return function_(
                Bucket=bucket,
                Key=key,
                # it's OK if limit > size
                # but it's not OK if to request byte 0 of an empty file
                Range=f"bytes=0-{limit}",
                VersionId=version_id
            )
        # else
        return function_(
            Bucket=bucket,
            Key=key,
            Range=f"0-{limit}",
            IfMatch=etag
        )

    return call()

def trim_to_bytes(string, limit=DOC_SIZE_LIMIT_BYTES):
    """trim string to specified number of bytes"""
    encoded = string.encode("utf-8")
    size = len(encoded)
    if size <= limit:
        return string
    return encoded[:limit].decode("utf-8", "ignore")

def _validate_kwargs(kwargs, required=("bucket", "key", "etag", "version_id")):
    """check for the existence of necessary object metadata in kwargs dict"""
    for word in required:
        if word not in kwargs:
            raise TypeError(f"Missing required keyword argument: {word}")
