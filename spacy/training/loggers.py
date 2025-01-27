from typing import TYPE_CHECKING, Dict, Any, Tuple, Callable, List, Optional, IO
from wasabi import Printer
import tqdm
import sys

from ..util import registry, flatten_dictionary, get_filtered_config
from .. import util
from ..errors import Errors

if TYPE_CHECKING:
    from ..language import Language  # noqa: F401


def setup_table(
    *, cols: List[str], widths: List[int], max_width: int = 13
) -> Tuple[List[str], List[int], List[str]]:
    final_cols = []
    final_widths = []
    for col, width in zip(cols, widths):
        if len(col) > max_width:
            col = col[: max_width - 3] + "..."  # shorten column if too long
        final_cols.append(col.upper())
        final_widths.append(max(len(col), width))
    return final_cols, final_widths, ["r" for _ in final_widths]


@registry.loggers("spacy.ConsoleLogger.v1")
def console_logger(progress_bar: bool = False):
    def setup_printer(
        nlp: "Language", stdout: IO = sys.stdout, stderr: IO = sys.stderr
    ) -> Tuple[Callable[[Optional[Dict[str, Any]]], None], Callable[[], None]]:
        write = lambda text: stdout.write(f"{text}\n")
        msg = Printer(no_print=True)
        # ensure that only trainable components are logged
        logged_pipes = [
            name
            for name, proc in nlp.pipeline
            if hasattr(proc, "is_trainable") and proc.is_trainable
        ]
        eval_frequency = nlp.config["training"]["eval_frequency"]
        score_weights = nlp.config["training"]["score_weights"]
        score_cols = [col for col, value in score_weights.items() if value is not None]
        loss_cols = [f"Loss {pipe}" for pipe in logged_pipes]
        spacing = 2
        table_header, table_widths, table_aligns = setup_table(
            cols=["E", "#"] + loss_cols + score_cols + ["Score"],
            widths=[3, 6] + [8 for _ in loss_cols] + [6 for _ in score_cols] + [6],
        )
        write(msg.row(table_header, widths=table_widths, spacing=spacing))
        write(msg.row(["-" * width for width in table_widths], spacing=spacing))
        progress = None

        def log_step(info: Optional[Dict[str, Any]]) -> None:
            nonlocal progress

            if info is None:
                # If we don't have a new checkpoint, just return.
                if progress is not None:
                    progress.update(1)
                return
            losses = [
                "{0:.2f}".format(float(info["losses"][pipe_name]))
                for pipe_name in logged_pipes
            ]

            scores = []
            for col in score_cols:
                score = info["other_scores"].get(col, 0.0)
                try:
                    score = float(score)
                except TypeError:
                    err = Errors.E916.format(name=col, score_type=type(score))
                    raise ValueError(err) from None
                if col != "speed":
                    score *= 100
                scores.append("{0:.2f}".format(score))

            data = (
                [info["epoch"], info["step"]]
                + losses
                + scores
                + ["{0:.2f}".format(float(info["score"]))]
            )
            if progress is not None:
                progress.close()
            write(
                msg.row(data, widths=table_widths, aligns=table_aligns, spacing=spacing)
            )
            if progress_bar:
                # Set disable=None, so that it disables on non-TTY
                progress = tqdm.tqdm(
                    total=eval_frequency, disable=None, leave=False, file=stderr
                )
                progress.set_description(f"Epoch {info['epoch']+1}")

        def finalize() -> None:
            pass

        return log_step, finalize

    return setup_printer


@registry.loggers("spacy.WandbLogger.v1")
def wandb_logger(project_name: str, remove_config_values: List[str] = []):
    try:
        import wandb
        from wandb import init, log, join  # test that these are available
    except ImportError:
        raise ImportError(Errors.E880.format(library="wandb", logger="WandbLogger")) from None

    console = console_logger(progress_bar=False)

    def setup_logger(
        nlp: "Language", stdout: IO = sys.stdout, stderr: IO = sys.stderr
    ) -> Tuple[Callable[[Dict[str, Any]], None], Callable[[], None]]:

        config = get_filtered_config(nlp, remove_config_values)

        wandb.init(project=project_name, config=config, reinit=True)
        console_log_step, console_finalize = console(nlp, stdout, stderr)

        def log_step(info: Optional[Dict[str, Any]]):
            console_log_step(info)
            if info is not None:
                score = info["score"]
                other_scores = info["other_scores"]
                losses = info["losses"]
                wandb.log({"score": score})
                if losses:
                    wandb.log({f"loss_{k}": v for k, v in losses.items()})
                if isinstance(other_scores, dict):
                    wandb.log(other_scores)

        def finalize() -> None:
            console_finalize()
            wandb.join()

        return log_step, finalize

    return setup_logger


@registry.loggers("spacy.CometLogger.v1")
def comet_logger(project_name: str, remove_config_values: List[str] = []):
    try:
        import comet_ml
    except ImportError:
        raise ImportError(Errors.E880.format(library="comet_ml", logger="CometLogger")) from None

    console = console_logger(progress_bar=False)

    def setup_logger(
        nlp: "Language", stdout: IO = sys.stdout, stderr: IO = sys.stderr
    ) -> Tuple[Callable[[Dict[str, Any]], None], Callable[[], None]]:

        try:
            experiment = comet_ml.Experiment(project_name=project_name)
        except Exception:
            experiment = None
            raise ValueError(Errors.E881.format(
                library="Comet", url="https://comet.ml/docs/python-sdk/spacy/")) from None

        config = get_filtered_config(nlp, remove_config_values)

        if experiment is not None:
            experiment.log_asset_data(config, "spacy-config.cfg")

        # Get methods for step console processing:
        console_log_step, console_finalize = console(nlp, stdout, stderr)

        def log_step(info: Optional[Dict[str, Any]]):
            console_log_step(info)
            if experiment is not None:
                if info is not None:
                    # Log items:
                    epoch = info.get("epoch", None)
                    step = info.get("step", None)
                    if "score" in info:
                        experiment.log_metric("score", info["score"], step=step, epoch=epoch)
                    if "other_scores" in info:
                        results = {}
                        flatten_dictionary("", info["other_scores"], results)
                        experiment.log_metrics(results, step=step, epoch=epoch)
                    if "losses" in info:
                        experiment.log_metrics({"loss_%s" % k: v for (k, v) in info["losses"].items()}, step=step, epoch=epoch)

        def finalize() -> None:
            if experiment is not None:
                experiment.end()
            console_finalize()

        return log_step, finalize

    return setup_logger
