import logging
import sys

from .forumdl import ForumDl
from . import options

from .session import SessionOptions
from .extractors.common import ExtractorOptions
from .writers.common import WriterOptions


def main():
    parser = options.build_parser()
    args = parser.parse_args()

    logging.basicConfig()
    logging.getLogger().setLevel(args.loglevel)

    forumdl = ForumDl()

    if args.list_extractors:
        print("\n".join(forumdl.list_extractors()))
    elif args.list_output_formats:
        print("\n".join(forumdl.list_output_formats()))
    elif not args.urls:
        parser.error(
            "The following arguments are required: URL\n"
            "Use 'forum-dl --help' to get a list of all options."
        )
    else:
        warc_output = args.output if args.output_format == "warc" else args.warc_output
        write_outside_file_objects = args.outside_files or bool(warc_output)

        forumdl.download(
            urls=args.urls,
            output_format=args.output_format,
            session_options=SessionOptions(
                timeout=float(args.timeout),
                download_timeout=float(args.download_timeout),
                retries=int(args.retries),
                retry_sleep=float(args.retry_sleep),
                retry_sleep_multiplier=float(args.retry_sleep_multiplier),
                warc_output=warc_output,
                user_agent=args.user_agent,
                get_urls=args.get_urls,
                cookies=args.cookies,
                headers=args.headers,
                header=args.header,
            ),
            extractor_options=ExtractorOptions(
                path=False,
            ),
            writer_options=WriterOptions(
                output_path=args.output,
                files_output_path=args.files_output,
                write_board_objects=args.boards,
                write_thread_objects=args.threads,
                write_post_objects=args.posts,
                write_file_objects=args.files,
                write_outside_file_objects=write_outside_file_objects,
                textify=args.textify,
                content_as_title=args.content_as_title,
                author_as_addr_spec=args.author_as_addr_spec,
            ),
        )
