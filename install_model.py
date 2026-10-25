import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        description="Install an Argos Translate language model."
    )
    parser.add_argument(
        "--from",
        dest="source_language",
        default="en",
        help="source language code (default: en)",
    )
    parser.add_argument(
        "--to",
        dest="target_language",
        default="hi", 
        help="target language code (default: hi)",
    )
    args = parser.parse_args()

    try:
        import argostranslate.package
    except ModuleNotFoundError as exc:
        if exc.name != "argostranslate":
            raise
        print(
            "Argos Translate is not installed. Run "
            "'python -m pip install -r requirements.txt' first.",
            file=sys.stderr,
        )
        return 1

    installed_package = next(
        (
            item
            for item in argostranslate.package.get_installed_packages()
            if item.from_code == args.source_language
            and item.to_code == args.target_language
        ),
        None,
    )
    if installed_package is not None:
        print(
            f"Translation model {args.source_language} -> "
            f"{args.target_language} is already installed."
        )
        return 0

    print("Checking available translation models...")
    argostranslate.package.update_package_index()
    packages = argostranslate.package.get_available_packages()

    package = next(
        (
            item
            for item in packages
            if item.from_code == args.source_language
            and item.to_code == args.target_language
        ),
        None,
    )

    if package is None:
        print(
            f"No model is available for "
            f"{args.source_language} -> {args.target_language}.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Downloading and installing "
        f"{args.source_language} -> {args.target_language}..."
    )
    download_path = package.download()
    argostranslate.package.install_from_path(download_path)
    print(
        f"Installed translation model: "
        f"{args.source_language} -> {args.target_language}."
    )
    return 0


if __name__ == "__main__":
    
    raise SystemExit(main())
