"""Tools for evaluation MIL models."""

import inspect
import os
import pandas as pd
import slideflow as sf
import numpy as np
import torch
import logging
from sklearn.metrics import roc_auc_score, average_precision_score, mean_absolute_error, mean_squared_error
from lifelines.utils import concordance_index
from sklearn.preprocessing import OneHotEncoder

from rich.progress import Progress, track
from os.path import join, exists, dirname
from typing import Union, List, Optional, Callable, Tuple, Any, TYPE_CHECKING
from slideflow import Dataset, log, errors
from slideflow.util import path_to_name, location_heatmap
from slideflow.model.extractors import rebuild_extractor
from slideflow.stats.metrics import ClassifierMetrics
from ._params import (
    _TrainerConfig, TrainerConfigFastAI
)
from . import utils

if TYPE_CHECKING:
    import torch
    from .features import MILFeatures
    from slideflow.norm import StainNormalizer
    from slideflow.model.base import BaseFeatureExtractor

# -----------------------------------------------------------------------------

def eval_mil(
    weights: str,
    dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: Union[str, List[str]],
    config: Optional[_TrainerConfig] = None,
    *,
    outdir: str = 'mil',
    attention_heatmaps: bool = False,
    uq: bool = False,
    aggregation_level: Optional[str] = None,
    **heatmap_kwargs
) -> pd.DataFrame:
    """Evaluate a multiple-instance learning model.

    Saves results for the evaluation in the target folder, including
    predictions (parquet format), attention (Numpy format for each slide),
    and attention heatmaps (if ``attention_heatmaps=True``).

    Logs classifier metrics (AUROC and AP) to the console.

    Args:
        weights (str): Path to model weights to load.
        dataset (sf.Dataset): Dataset to evaluation.
        outcomes (str, list(str)): Outcomes.
        bags (str, list(str)): Path to bags, or list of bag file paths.
            Each bag should contain PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for building model. If ``weights`` is a path to a
            model directory, will attempt to read ``mil_params.json`` from this
            location and load saved configuration. Defaults to None.

    Keyword arguments:
        outdir (str): Path at which to save results.
        attention_heatmaps (bool): Generate attention heatmaps for slides.
            Not available for multi-modal MIL models. Defaults to False.
        interpolation (str, optional): Interpolation strategy for smoothing
            attention heatmaps. Defaults to 'bicubic'.
        cmap (str, optional): Matplotlib colormap for heatmap. Can be any
            valid matplotlib colormap. Defaults to 'inferno'.
        norm (str, optional): Normalization strategy for assigning heatmap
            values to colors. Either 'two_slope', or any other valid value
            for the ``norm`` argument of ``matplotlib.pyplot.imshow``.
            If 'two_slope', normalizes values less than 0 and greater than 0
            separately. Defaults to None.

    """
    #log heatmap kwargs in eval_mil
    logging.debug(f"Heatmap kwargs in eval_mil: {heatmap_kwargs}")
    model, config = utils.load_model_weights(weights, config, **heatmap_kwargs)

    params = {
        'model_path': weights,
        'eval_bags': bags,
        'eval_filters': dataset._filters,
        'mil_params': sf.util.load_json(join(weights, 'mil_params.json'))
    }

    eval_kwargs = dict(
        dataset=dataset,
        outcomes=outcomes,
        bags=bags,
        config=config,
        outdir=outdir,
        params=params
    )

    if aggregation_level is not None:
        if aggregation_level not in ('patient', 'slide'):
            raise ValueError(
                f"Unrecognized aggregation level: '{aggregation_level}'. "
                "Must be either 'patient' or 'slide'."
            )
        config.aggregation_level = aggregation_level

    if config.is_multimodal:
        heatmap_kwargs = None
        #Add pb_config to eval_kwargs
        eval_kwargs['pb_config'] = pb_config
        if attention_heatmaps:
            raise ValueError(
                "Attention heatmaps cannot yet be exported for multi-modal "
                "models. Please use Slideflow Studio for visualization of "
                "multi-modal attention."
            )
        if heatmap_kwargs:
            kwarg_names = ', '.join(list(heatmap_kwargs.keys()))
            raise ValueError(
                f"Unrecognized keyword arguments: '{kwarg_names}'. Attention "
                "heatmap keyword arguments are not currently supported for "
                "multi-modal models."
            )
        
        return _eval_multimodal_mil(model, **eval_kwargs)
    else:
        return _eval_mil(
            model,
            attention_heatmaps=attention_heatmaps,
            uq=uq,
            **heatmap_kwargs,
            **eval_kwargs
        )


def _eval_mil(
    model: "torch.nn.Module",
    dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: Union[str, List[str]],
    config: _TrainerConfig,
    *,
    outdir: str = 'mil',
    attention_heatmaps: bool = False,
    uq: bool = False,
    params: Optional[dict] = None,
    **heatmap_kwargs
) -> pd.DataFrame:
    """Evaluate a standard, single-mode multi-instance learning model.

    Args:
        model (torch.nn.Module): Loaded PyTorch MIL model.
        dataset (sf.Dataset): Dataset to evaluation.
        outcomes (str, list(str)): Outcomes.
        bags (str, list(str)): Path to bags, or list of bag file paths.
            Each bag should contain PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for building model.

    Keyword arguments:
        outdir (str): Path at which to save results.
        attention_heatmaps (bool): Generate attention heatmaps for slides.
            Defaults to False.
        interpolation (str, optional): Interpolation strategy for smoothing
            attention heatmaps. Defaults to 'bicubic'.
        cmap (str, optional): Matplotlib colormap for heatmap. Can be any
            valid matplotlib colormap. Defaults to 'inferno'.
        norm (str, optional): Normalization strategy for assigning heatmap
            values to colors. Either 'two_slope', or any other valid value
            for the ``norm`` argument of ``matplotlib.pyplot.imshow``.
            If 'two_slope', normalizes values less than 0 and greater than 0
            separately. Defaults to None.

    Returns:
        pd.DataFrame: Dataframe of predictions.
    """

    task = config.to_dict().get('goal', config.to_dict().get('task', 'classification'))

    # Prepare lists of bags.
    labels, _ = dataset.labels(outcomes, format='id')
    logging.debug(f"Labels in eval: {labels}")
    slides = list(labels.keys())
    if isinstance(bags, str):
        bags = dataset.pt_files(bags)
    else:
        bags = np.array([b for b in bags if path_to_name(b) in slides])

    logging.debug(f"bags: {bags}")
    # Generate predictions.
    df, y_att = predict_from_model(
        model,
        config,
        dataset,
        outcomes=outcomes,
        bags=bags,
        attention=True,
        uq=uq,
        **heatmap_kwargs
    )


    if task == 'survival':
        # Calculate the concordance index for survival analysis
        #Log the types and values of the columns in df
        log.debug(f"DataFrame columns: {df.columns.tolist()}")
        log.debug("DataFrame types:")
        for col in df.columns:
            log.debug(f"{col}: {df[col].dtype}")

        #Ensure duration is of type float, event is integer and y_pred0 is float
        df['duration'] = df['duration'].astype(float)
        df['y_true'] = df['y_true'].astype(int)
        df['y_pred0'] = df['y_pred0'].astype(float)
        log.debug("DataFrame after type conversion:")
        log.debug(df.head())
        
        c_index = concordance_index(df['duration'], -df['y_pred0'], df['y_true'])
        df['c_index'] = c_index

    elif task == 'survival_discrete':
        all_time_labels = np.unique(df.duration)
        pred_scores = []
        #Each bin should be a ground truth label, and we should have a prediction for each bin
        for idx in range(len(all_time_labels)):
            y_true_binary = (df['duration'] == all_time_labels[idx]).astype(int)
            y_pred = df[f'y_pred{idx}'].values
            pred_scores.append(y_pred)
            try:
                auc = roc_auc_score(y_true_binary, y_pred)
            except ValueError as e:
                logging.warning(f"Skipping AUC for cat #{idx+1}: {e}")
                auc = np.nan
            ap = average_precision_score(y_true_binary, y_pred)
            log.info(f"AUC (time={all_time_labels[idx]}): {auc:.3f}")
            log.info(f"AP  (time={all_time_labels[idx]}): {ap:.3f}")

        c_index = concordance_index(df['duration'], pred_scores, event_observed=df['y_true'])
        df['c_index'] = c_index
        
    elif task == 'regression':
        # Calculate regression metrics
        mae = mean_absolute_error(df['y_true'], df['y_pred0'])
        mse = mean_squared_error(df['y_true'], df['y_pred0'])
        log.info(f"Mean Absolute Error: {mae:.3f}")
        log.info(f"Mean Squared Error: {mse:.3f}")
    else:
        # Calculate metrics for classification
        y_pred_cols = [c for c in df.columns if c.startswith('y_pred')]
        for idx in range(len(y_pred_cols)):
            y_true_binary = (df.y_true.values == idx).astype(int)
            y_pred = df[f'y_pred{idx}'].values
            try:
                auc = roc_auc_score(y_true_binary, y_pred)
            except ValueError as e:
                logging.warning(f"Skipping AUC for cat #{idx+1}: {e}")
                auc = np.nan
            ap = average_precision_score(y_true_binary, y_pred)
            log.info(f"AUC (cat #{idx+1}): {auc:.3f}")
            log.info(f"AP  (cat #{idx+1}): {ap:.3f}")

    # Save results.
    if outdir:
        if not exists(outdir):
            os.makedirs(outdir)
        model_dir = sf.util.get_new_model_dir(outdir, config.model_config.model)
        if params is not None:
            sf.util.write_json(params, join(model_dir, 'mil_params.json'))
        pred_out = join(model_dir, 'predictions.parquet')
        df.to_parquet(pred_out)
        log.info(f"Predictions saved to {pred_out}")

    # Print categorical metrics, including per-category accuracy
    outcome_name = outcomes if isinstance(outcomes, str) else '-'.join(outcomes)
    metrics_df = df.rename(
        columns={c: f"{outcome_name}-{c}" for c in df.columns if c != 'slide'}
    )
    sf.stats.metrics.categorical_metrics(metrics_df, level='slide')

    # Export attention
    if outdir and y_att:
        if 'slide' in df.columns:
            slides_or_patients = df.slide.values
        elif 'patient' in df.columns:
            slides_or_patients = df.patient.values
        else:
            raise ValueError("Malformed dataframe; cannot find 'slide' or 'patient' column.")
        _export_attention(join(model_dir, 'attention'), y_att, slides_or_patients)

    # Attention heatmaps
    if outdir and y_att and attention_heatmaps:
        generate_attention_heatmaps(
            outdir=join(model_dir, 'heatmaps'),
            dataset=dataset,
            bags=bags,  # type: ignore
            attention=y_att,
            **heatmap_kwargs
        )

    return df


def _eval_multimodal_mil(
    model: "torch.nn.Module",
    dataset: Dataset,
    outcomes: Union[str, List[str]],
    bags: List[List[str]],
    config: _TrainerConfig,
    *,
    outdir: str = 'mil',
    params: Optional[dict] = None,
    **kwargs
) -> pd.DataFrame:
    """Evaluate a multi-modal (e.g. multi-magnification) MIL model.

    Args:
        model (torch.nn.Module): Loaded PyTorch MIL model.
        dataset (sf.Dataset): Dataset for evaluation.
        outcomes (str, list(str)): Outcomes.
        bags (str, list(str)): Path to bags, or list of bag file paths.
            Each bag should contain a PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for building the model.

    Keyword arguments:
        outdir (str): Path at which to save results.

    Returns:
        pd.DataFrame: Dataframe of predictions.
    """
    # Prepare ground-truth labels
    labels, unique = dataset.labels(outcomes, format='id')

    # Prepare bags and targets
    bags, slides = utils._get_nested_bags(dataset, bags)
    y_true = np.array([labels[s] for s in slides])

    # Inference
    y_pred, y_att = _predict_multimodal_mil(
        model, bags, attention=True, use_lens=config.model_config.use_lens
    )

    # Evaluate based on the task
    task = config.to_dict()['goal']
    if task == 'survival' or task == 'survival_discrete':

        if task == 'survival_discrete':
            all_time_labels = np.unique(y_true)
            pred_scores = []
            for idx in range(len(all_time_labels)):
                y_true_binary = (y_true == all_time_labels[idx]).astype(int)
                y_pred_binary = y_pred[:, idx]
                pred_scores.append(y_pred_binary)
                auc = roc_auc_score(y_true_binary, y_pred_binary)
                ap = average_precision_score(y_true_binary, y_pred_binary)
                log.info(f"AUC (time={all_time_labels[idx]}): {auc:.3f}")
                log.info(f"AP  (time={all_time_labels[idx]}): {ap:.3f}")
            #Compute weighted average
            pred_scores = (pred_scores * all_time_labels).sum(axis=1)
            c_index = concordance_index(y_true[:, 0], pred_scores, event_observed=y_true[:, 1])
            log.info(f"Concordance Index: {c_index:.3f}")
            metrics = {'c_index': c_index}
        else:
            # Calculate the concordance index for survival analysis
            c_index = concordance_index(y_true[:, 0], y_pred[:, 0], event_observed=y_true[:, 1])
            log.info(f"Concordance Index: {c_index:.3f}")
            metrics = {'c_index': c_index}
    elif task == 'regression':
        # Calculate regression metrics
        mae = mean_absolute_error(y_true, y_pred[:, 0])
        mse = mean_squared_error(y_true, y_pred[:, 0])
        log.info(f"Mean Absolute Error: {mae:.3f}")
        log.info(f"Mean Squared Error: {mse:.3f}")
        metrics = {'mae': mae, 'mse': mse}
    else:
        # Calculate metrics for classification
        metrics = {}
        for idx in range(y_pred.shape[-1]):
            y_true_binary = (y_true == idx).astype(int)
            y_pred_binary = y_pred[:, idx]
            auc = roc_auc_score(y_true_binary, y_pred_binary)
            ap = average_precision_score(y_true_binary, y_pred_binary)
            log.info(f"AUC (cat #{idx+1}): {auc:.3f}")
            log.info(f"AP  (cat #{idx+1}): {ap:.3f}")
            metrics[f'auc_{idx}'] = auc
            metrics[f'ap_{idx}'] = ap

    # Assemble dataframe
    df_dict = dict(slide=slides, y_true=y_true)
    for i in range(y_pred.shape[-1]):
        df_dict[f'y_pred{i}'] = y_pred[:, i]
    df = pd.DataFrame(df_dict)

    # Save results
    if outdir:
        if not os.path.exists(outdir):
            os.makedirs(outdir)
        model_dir = sf.util.get_new_model_dir(outdir, config.model_config.model)
        if params is not None:
            sf.util.write_json(params, os.path.join(model_dir, 'mil_params.json'))
        pred_out = os.path.join(model_dir, 'predictions.parquet')
        df.to_parquet(pred_out)
        log.info(f"Predictions saved to [green]{pred_out}[/]")

    # Export attention
    if outdir and y_att:
        _export_attention(os.path.join(model_dir, 'attention'), y_att, df.slide.values)

    return df

# -----------------------------------------------------------------------------

def predict_slide(
    model: str,
    slide: Union[str, sf.WSI],
    extractor: Optional["BaseFeatureExtractor"] = None,
    *,
    normalizer: Optional["StainNormalizer"] = None,
    config: Optional[_TrainerConfig] = None,
    attention: bool = False,
    native_normalizer: Optional[bool] = True,
    extractor_kwargs: Optional[dict] = None,
    heatmap_kwargs: Optional[dict] = None,
    **kwargs
) -> Tuple[np.ndarray, Optional[np.ndarray]]:
    """Generate predictions (and attention) for a single slide.

    Args:
        model (str): Path to MIL model.
        slide (str): Path to slide.
        extractor (:class:`slideflow.mil.BaseFeatureExtractor`, optional):
            Feature extractor. If not provided, will attempt to auto-detect
            extractor from model.

            .. note::
                If the extractor has a stain normalizer, this will be used to
                normalize the slide before extracting features.

    Keyword Args:
        normalizer (:class:`slideflow.stain.StainNormalizer`, optional):
            Stain normalizer. If not provided, will attempt to use stain
            normalizer from extractor.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for building model. If None, will attempt to read
            ``mil_params.json`` from the model directory and load saved
            configuration. Defaults to None.
        attention (bool): Whether to return attention scores. Defaults to
            False.
        native_normalizer (bool, optional): Whether to use PyTorch/Tensorflow-native
            stain normalization, if applicable. If False, will use the OpenCV/Numpy
            implementations. Defaults to None, which auto-detects based on the
            slide backend (False if libvips, True if cucim). This behavior is due
            to performance issued when using native stain normalization with
            libvips-compatible multiprocessing.

    Returns:
        Tuple[np.ndarray, Optional[np.ndarray]]: Predictions and attention scores.
        Attention scores are None if ``attention`` is False, otherwise
        a masked 2D array with the same shape as the slide grid (arranged as a
        heatmap, with unused tiles masked).

    """
    # Try to auto-determine the extractor
    if native_normalizer is None:
        native_normalizer = (sf.slide_backend() == 'cucim')
    if extractor is None:
        extractor, detected_normalizer = rebuild_extractor(
            model, allow_errors=True, native_normalizer=native_normalizer
        )
        if extractor is None:
            raise ValueError(
                "Unable to auto-detect feature extractor used for model {}. "
                "Please specify an extractor.".format(model)
            )
    else:
        detected_normalizer = None

    # Determine stain normalization
    if detected_normalizer is not None and normalizer is not None:
        log.warning(
            "Bags were generated with a stain normalizer, but a different stain "
            "normalizer was provided to this function. Overriding with provided "
            "stain normalizer."
        )
    elif detected_normalizer is not None:
        normalizer = detected_normalizer

    model_fn, config = utils.load_model_weights(model, config, **kwargs)
    mil_params = sf.util.load_json(join(model, 'mil_params.json'))
    if 'bags_extractor' not in mil_params:
        raise ValueError(
            "Unable to determine extractor used for model {}. "
            "Please specify an extractor.".format(model)
        )
    bags_params = mil_params['bags_extractor']

    # Load slide
    if isinstance(slide, str):
        if not all(k in bags_params for k in ('tile_px', 'tile_um')):
            raise ValueError(
                "Unable to determine tile size for slide {}. "
                "Either slide must be a slideflow.WSI object, or tile_px and "
                "tile_um must be specified in mil_params.json.".format(slide)
            )
        slide_name = slide
        slide = sf.WSI(
            slide,
            tile_px=bags_params['tile_px'],
            tile_um=bags_params['tile_um']
        )

    # Convert slide to bags
    if extractor_kwargs is None:
        extractor_kwargs = dict()
    masked_bags = extractor(slide, normalizer=normalizer, **extractor_kwargs)
    original_shape = masked_bags.shape
    masked_bags = masked_bags.reshape((-1, masked_bags.shape[-1]))
    if len(masked_bags.mask.shape):
        mask = masked_bags.mask.any(axis=1)
        valid_indices = np.where(~mask)
        bags = masked_bags[valid_indices]
    else:
        valid_indices = np.arange(masked_bags.shape[0])
        bags = masked_bags
    bags = np.expand_dims(bags, axis=0).astype(np.float32)

    sf.log.info("Generated feature bags for {} tiles".format(bags.shape[1]))


    y_pred, raw_att = _predict_mil(
        model_fn,
        bags,
        attention=attention,
        use_lens=False,
        apply_softmax=config.model_config.apply_softmax
    )

    if attention and raw_att is not None and len(raw_att):
        raw = raw_att[0]  # this may be length = n_tiles or n_heads * n_tiles
        # figure out how many valid tiles we had
        # valid_indices is either a tuple from np.where or an array
        if isinstance(valid_indices, tuple):
            valid_count = valid_indices[0].shape[0]
        else:
            valid_count = valid_indices.shape[0]

        # detect multi-head: is raw longer than valid_count and divisible?
        if raw.ndim == 1 and raw.shape[0] > valid_count and raw.shape[0] % valid_count == 0:
            num_heads = raw.shape[0] // valid_count
            # reshape into (n_heads, n_tiles)
            heads_att = raw.reshape((num_heads, valid_count))
            # compute per-tile average
            avg_att = heads_att.mean(axis=0)
            # prepare a list of (name, values) for avg + each head
            heatmaps = [("avg", avg_att)]
            heatmaps += [(f"head_{h}", heads_att[h]) for h in range(num_heads)]
        else:
            # single-head case
            heatmaps = [("avg", raw)]

        df = slide.get_tile_dataframe().sort_values(['grid_y','grid_x'])
        centers = df[['loc_x','loc_y']].values

        avg_hm = None
        for name, att_vals in heatmaps:
            # 1) make a full-length masked array:
            full = np.ma.masked_all(masked_bags.shape[0], dtype=att_vals.dtype)
            # 2) fill in just the valid positions:
            full[valid_indices] = att_vals
            # 3) reshape to 2D grid
            hm2d = full.reshape(original_shape[:2])

            # save the average headed map for return
            if name == "avg":
                avg_hm = hm2d

            # 4) plot/save each head (and avg) as before
            flat = hm2d.ravel()
            valid = ~hm2d.mask.ravel()
            locs = centers[valid]
            vals = flat[valid]

            #Make sure outdir exists
            os.makedirs(f"inference_results/{name}", exist_ok=True)

            location_heatmap(
                locations=locs,
                values=vals,
                slide=slide_name,
                tile_px=bags_params['tile_px'],
                tile_um=bags_params['tile_um'],
                outdir=f"inference_results/{name}",
                norm = heatmap_kwargs.get('norm', None),
                interpolation=heatmap_kwargs.get('interpolation', 'bicubic'),
                cmap=heatmap_kwargs.get('cmap', 'inferno')
            )

        y_att = avg_hm

    else:
        y_att = None

    return y_pred, y_att


def get_mil_tile_predictions(
    weights: str,
    dataset: "sf.Dataset",
    bags: Union[str, np.ndarray, List[str]],
    *,
    config: Optional[_TrainerConfig] = None,
    outcomes: Union[str, List[str]] = None,
    dest: Optional[str] = None,
    uq: bool = False
) -> pd.DataFrame:
    # Load model and configuration.
    model, config = utils.load_model_weights(weights, config)
    if outcomes is not None:
        labels, unique = dataset.labels(outcomes, format='id')

    # Prepare bags.
    slides = dataset.slides()
    if isinstance(bags, str):
        bags = dataset.pt_files(bags)
    else:
        bags = np.array([b for b in bags if path_to_name(b) in slides])

    # Ensure slide names are sorted according to the bags.
    slides = [path_to_name(b) for b in bags]


    print("Generating predictions for {} slides and {} bags.".format(len(slides), len(bags)))


    slide_pred, attention = _predict_mil(
        model,
        bags,
        attention=True,
        use_lens=config.model_config.use_lens,
        apply_softmax=config.model_config.apply_softmax
    )

    df_slides = []
    df_attention = []
    df_preds = []
    df_uq = []
    df_true = []
    df_loc_x = []
    df_loc_y = []

    # Then, generate tile predictions for each slide:
    for i, (bag, slide) in track(enumerate(zip(bags, slides)),
                            description="Generating tile predictions",
                            total=len(bags)):
        pred_out = _predict_mil_tiles(
            model,
            bag,
            use_lens=config.model_config.use_lens,
            apply_softmax=config.model_config.apply_softmax,
            uq=uq
        )
        if uq:
            tile_pred, tile_att, tile_uq = pred_out
        else:
            tile_pred, tile_att = pred_out

        # Verify the shapes are consistent.
        assert len(tile_pred) == len(attention[i])
        n_bags = len(tile_pred)

        # Find the associated locations.
        bag_index = join(dirname(bag), f'{slide}.index.npz')
        if exists(bag_index):
            locations = np.load(bag_index)['arr_0']
            assert len(locations) == n_bags
            df_loc_x.append(locations[:, 0])
            df_loc_y.append(locations[:, 1])

        # Add to dataframe lists.
        df_preds.append(tile_pred)
        if uq:
            df_uq.append(tile_uq)
        if attention is not None:
            df_attention.append(attention[i])
        df_slides += [slide for _ in range(n_bags)]
        if outcomes is not None:
            _label = labels[slide]
            df_true += [_label for _ in range(n_bags)]

    # Update dataframe with predictions.
    df_dict = dict(slide=df_slides)
    df_attention = np.concatenate(df_attention, axis=0)
    df_preds = np.concatenate(df_preds, axis=0)

    # Tile location
    if df_loc_x:
        df_dict['loc_x'] = np.concatenate(df_loc_x, axis=0)
        df_dict['loc_y'] = np.concatenate(df_loc_y, axis=0)

    # Attention
    if attention is not None:
        df_dict['attention'] = df_attention

    # Uncertainty
    if uq:
        df_uq = np.concatenate(df_uq, axis=0)
        for i in range(df_uq[0].shape[0]):
            df_dict[f'uncertainty{i}'] = df_uq[:, i]

    # Ground truth
    if outcomes is not None:
        df_dict['y_true'] = df_true

    # Predictions
    for i in range(df_preds[0].shape[0]):
        df_dict[f'y_pred{i}'] = df_preds[:, i]

    # Final processing to dataframe & disk
    df = pd.DataFrame(df_dict)
    if dest is not None:
        df.to_parquet(dest)
        log.info("{} tile predictions exported to [green]{}[/]".format(
            df_preds.shape[0],
            dest
        ))
    return df


def save_mil_tile_predictions(
    weights: str,
    dataset: "sf.Dataset",
    bags: Union[str, np.ndarray, List[str]],
    config: Optional[_TrainerConfig] = None,
    outcomes: Union[str, List[str]] = None,
    dest: str = 'mil_tile_preds.parquet',
) -> pd.DataFrame:
    return get_mil_tile_predictions(
        weights,
        dataset,
        bags,
        config=config,
        outcomes=outcomes,
        dest=dest
    )


def predict_from_model(
    model: Callable,
    config: _TrainerConfig,
    dataset: "sf.Dataset",
    outcomes: Union[str, List[str]],
    bags: Union[str, np.ndarray, List[str]],
    *,
    attention: bool = False,
    uq: bool = False,
    **kwargs
) -> Union[pd.DataFrame, Tuple[pd.DataFrame, List[np.ndarray]]]:
    """Generate predictions for a dataset from a saved MIL model.

    Args:
        model (torch.nn.Module): Model from which to generate predictions.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for the MIL model.
        dataset (sf.Dataset): Dataset from which to generation predictions.
        outcomes (str, list(str)): Outcomes.
        bags (str, list(str)): Path to bags, or list of bag file paths.
            Each bag should contain PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.

    Returns:
        pd.DataFrame: Dataframe of predictions.

        list(np.ndarray): Attention scores (if ``attention=True``)
    """

    task = config.to_dict().get('goal', config.to_dict().get('task', 'classification'))
    logging.debug(f"Predicting from model for task: {task}")

    # Prepare labels.
    labels, unique = dataset.labels(outcomes, format='id')
    logging.debug(f"Labels: {labels}")
    logging.debug(f"Unique labels: {unique}")

    # Prepare bags and targets.
    slides = list(labels.keys())
    if isinstance(bags, str):
        bags = dataset.pt_files(bags)
    else:
        bags = np.array([b for b in bags if path_to_name(b) in slides])

    # Aggregate bags by slide or patient.
    if (isinstance(config, TrainerConfigFastAI)
        and config.aggregation_level == 'patient'):

        # Get nested list of bags, aggregated by slide.
        slide_to_patient = dataset.patients()
        n_slide_bags = len(bags)
        bags, y_true = utils.aggregate_bags_by_patient(bags, labels, slide_to_patient, task)
        logging.info(f"Aggregated {n_slide_bags} slide bags to {len(bags)} patient bags.")

        # Create prediction dataframe.
        patients = [slide_to_patient[path_to_name(b[0])] for b in bags]
        df_dict = dict(patient=patients, y_true=y_true)

    else:
        # Ensure slide names are sorted according to the bags.
        slides = [path_to_name(b) for b in bags]
        y_true = np.array([labels[s] for s in slides])

        # Create prediction dataframe.
        df_dict = dict(slide=slides, y_true=y_true)

    # Generate predictions.
    if task == 'survival' or task == 'regression':
        pred_out = _predict_mil(
            model,
            bags,
            attention=attention,
            use_lens=config.model_config.use_lens,
            apply_softmax=False,
            uq=uq
        )
    else:
        pred_out = _predict_mil(
            model,
            bags,
            attention=attention,
            use_lens=config.model_config.use_lens,
            apply_softmax=config.model_config.apply_softmax,
            uq=uq
        )
    if uq:
        y_pred, y_att, y_uq = pred_out
    else:
        y_pred, y_att = pred_out

    # Update dataframe with predictions.
    for i in range(y_pred.shape[-1]):
        value = y_pred[:, i]
        if isinstance(value, list):
            value = torch.tensor(value)
        if len(value.shape) > 1:
            value = value.squeeze()

        df_dict[f'y_pred{i}'] = value.numpy() if isinstance(value, torch.Tensor) else value
    
    if uq:
        for i in range(y_uq.shape[-1]):
            value = y_uq[:, i]
            if isinstance(value, list):
                value = torch.tensor(value)
            if len(value.shape) > 1:
                value = value.squeeze()

            df_dict[f'uncertainty{i}'] = value.numpy() if isinstance(value, torch.Tensor) else value
    
    #If survival labels, set to event only
    if task == 'survival' or task == 'survival_discrete':
        #check dimensionality of y_true
        if len(y_true.shape) > 1:
            df_dict['duration'] = y_true[:, 0].astype(np.float32)
            df_dict['y_true'] = y_true[:, 1].astype(np.int32)
        else:
            print(df_dict['y_true'])
            df_dict['y_true'] = y_true

        #Make sure y_pred0 is always present and float
        assert 'y_pred0' in df_dict, "y_pred0 must be present for survival analysis."
        df_dict['y_pred0'] = df_dict['y_pred0'].astype(np.float32)
        logging.debug(f"Survival labels: {df_dict['y_true']}")
        logging.debug(f"Survival durations: {df_dict['duration']}")

    df = pd.DataFrame(df_dict)


    if attention:
        return df, y_att
    else:
        return df


def generate_mil_features(
    weights: str,
    dataset: "sf.Dataset",
    bags: Union[str, np.ndarray, List[str]],
    *,
    config: Optional[_TrainerConfig] = None,
) -> "MILFeatures":
    """Generate activations weights from the last layer of an MIL model.

    Returns MILFeatures object.

    Args:
        weights (str): Path to model weights to load.
        config (:class:`slideflow.mil.TrainerConfigFastAI` or
        :class:`slideflow.mil.TrainerConfigCLAM`):
            Configuration for building model. If ``weights`` is a path to a
            model directory, will attempt to read ``mil_params.json`` from this
            location and load saved configuration. Defaults to None.
        dataset (:class:`slideflow.Dataset`): Dataset.
        outcomes (str, list(str)): Outcomes.
        bags (str, list(str)): Path to bags, or list of bag file paths.
            Each bag should contain PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.
    """
    from .features import MILFeatures

    # Load model weights.
    model, config = utils.load_model_weights(weights, config)

    # Ensure the model is valid for generating features.
    if not hasattr(model, 'get_last_layer_activations'):
        raise errors.ModelError(
            f"Model {config.model_config.model} is not supported.")

    # Prepare bags and targets.
    slides = dataset.slides()
    if isinstance(bags, str):
        bags = dataset.pt_files(bags)
    else:
        bags = np.array([b for b in bags if path_to_name(b) in slides])

    # Ensure slide names are sorted according to the bags.
    slides = [path_to_name(b) for b in bags]

    # Calculate and return last-layer features.
    return MILFeatures(model, bags, slides=slides, config=config, dataset=dataset)


def generate_attention_heatmaps(
    outdir: str,
    dataset: "sf.Dataset",
    bags: Union[List[str], np.ndarray],
    attention: Union[np.ndarray, List[np.ndarray]],
    **kwargs
) -> None:
    """Generate and save attention heatmaps for a dataset.

    Args:
        outdir (str): Path at which to save heatmap images.
        dataset (sf.Dataset): Dataset.
        bags (str, list(str)): List of bag file paths.
            Each bag should contain PyTorch array of features from all tiles in
            a slide, with the shape ``(n_tiles, n_features)``.
        attention (list(np.ndarray)): Attention scores for each slide.
            Length of ``attention`` should equal the length of ``bags``.

    Keyword args:
        interpolation (str, optional): Interpolation strategy for smoothing
            heatmap. Defaults to 'bicubic'.
        cmap (str, optional): Matplotlib colormap for heatmap. Can be any
            valid matplotlib colormap. Defaults to 'inferno'.
        norm (str, optional): Normalization strategy for assigning heatmap
            values to colors. Either 'two_slope', or any other valid value
            for the ``norm`` argument of ``matplotlib.pyplot.imshow``.
            If 'two_slope', normalizes values less than 0 and greater than 0
            separately. Defaults to None.


    """

    #Log the number of bags and attention scores
    log.info(f"Generating attention heatmaps for {len(bags)} bags and {len(attention)} attention scores.")
    if len(bags) != len(attention):
        logging.warning("Number of bags and attention scores do not match.")
        return
    assert len(bags) == len(attention)
    if not exists(outdir):
        os.makedirs(outdir)
    pb = Progress(transient=True)
    task = pb.add_task('Generating heatmaps...', total=len(bags))
    pb.start()
    with sf.util.cleanup_progress(pb):
        for i, bag in enumerate(bags):
            pb.advance(task)
            slidename = sf.util.path_to_name(bag)
            slide_path = dataset.find_slide(slide=slidename)
            locations_file = join(dirname(bag), f'{slidename}.index.npz')
            npy_loc_file = locations_file[:-1] + 'y'
            if slide_path is None:
                log.info(f"Unable to find slide {slidename}")
                continue
            if exists(locations_file):
                locations = np.load(locations_file)['arr_0']
            elif exists(npy_loc_file):
                locations = np.load(npy_loc_file)
            else:
                log.info(
                    f"Unable to find locations index file for {slidename}"
                )
                continue
            sf.util.location_heatmap(
                locations=locations,
                values=attention[i],
                slide=slide_path,
                tile_px=dataset.tile_px,
                tile_um=dataset.tile_um,
                outdir=outdir,
                **kwargs
            )
    log.info(f"Attention heatmaps saved to [green]{outdir}[/]")

# -----------------------------------------------------------------------------

def _export_attention(
    dest: str,
    y_att: Union[List[np.ndarray], List[List[np.ndarray]]],
    slides: List[str]
) -> None:
    """Export attention scores to a directory."""
    if not exists(dest):
        os.makedirs(dest)
    for slide, att in zip(slides, y_att):

        if isinstance(att, (list, tuple)) and not sf.util.zip_allowed():
            raise RuntimeError(
                "Cannot export multimodal attention scores to a directory (NPZ) "
                "when ZIP functionality is disabled. Enable zip functionality "
                "by setting 'SF_ALLOW_ZIP=1' in your environment, or by "
                "wrapping your script in 'with sf.util.enable_zip():'.")

        elif isinstance(att, (list, tuple)):
            out_path = join(dest, f'{slide}_att.npz')
            np.savez(out_path, *att)

        elif sf.util.zip_allowed():
            out_path = join(dest, f'{slide}_att.npz')
            np.savez(out_path, att)

        else:
            out_path = join(dest, f'{slide}_att.npy')
            np.save(out_path, att)

    log.info(f"Attention scores exported to [green]{out_path}[/]")


def _validate_model(
    model: "torch.nn.Module",
    attention: bool,
    uq: bool,
    *,
    allow_errors: bool = False
) -> Tuple[bool, bool]:
    """Validate that a model supports attention and/or UQ."""
    if attention and not hasattr(model, 'calculate_attention'):
        msg = (
            "Model '{}' does not have a method 'calculate_attention'. "
            "Unable to calculate or display attention heatmaps.".format(
                model.__class__.__name__
            )
        )
        attention = False
        if allow_errors:
            log.warning(msg)
        else:
            raise RuntimeError(msg)
    if uq and not inspect.signature(model.forward).parameters['uq']:
        msg = (
            "Model '{}' does not support UQ. "
            "Unable to calculate uncertainty.".format(
                model.__class__.__name__
            )
        )
        uq = False
        if allow_errors:
            log.warning(msg)
        else:
            raise RuntimeError(msg)
    return attention, uq


def run_inference(
    model: "torch.nn.Module",
    input: "torch.Tensor",
    *,
    attention: bool = False,
    attention_pooling: str = 'avg',
    uq: bool = False,
    use_first_out: bool = False,
    apply_softmax: bool = True,
    use_lens: bool = False,
    device: Optional[Any] = None
) -> Tuple[np.ndarray, Optional[np.ndarray], Optional[np.ndarray]]:
    """Run inference on a MIL model."""
    import torch

    y_pred, y_att, y_uncertainty = None, None, None

    # Prepare lens
    device = utils._detect_device(model, device, verbose=False)
    if isinstance(use_lens, bool) and use_lens:
        lens = torch.from_numpy(np.array([input.shape[1]])).to(device)
        model_args = (input, lens)
    elif use_lens is not False and use_lens is not None:
        model_args = (input, use_lens)
    else:
        model_args = (input,)

    if uq and inspect.signature(model.forward).parameters['uq']:
        kw = dict(uq=True)
    elif uq:
        raise RuntimeError("Model does not support UQ.")
    else:
        kw = dict()
    try:
        if attention and inspect.signature(model.forward).parameters['return_attention']:
            model_out, y_att = model(*model_args, return_attention=True, **kw)
        elif use_first_out:
            # CLAM models return attention scores as well as logits.
            model_out, y_att = model(*model_args, **kw)
        elif attention:
            model_out = model(*model_args, **kw)
            y_att = model.calculate_attention(*model_args)
        else:
            model_out = model(*model_args, **kw)
    except:
        print("No attention scores available.")
        if use_first_out:
            # CLAM models return attention scores as well as logits.
            model_out, y_att = model(*model_args, **kw)
        elif attention:
            model_out = model(*model_args, **kw)
            y_att = model.calculate_attention(*model_args)
        else:
            model_out = model(*model_args, **kw)


    # Parse uncertainty from model output.
    if uq:
        y_pred, y_uncertainty = model_out
    else:
        y_pred = model_out

    if attention:
        y_att = torch.squeeze(y_att)
        if len(y_att.shape) == 2:
            log.warning("Pooling attention scores from 2D to 1D")
            # Attention needs to be pooled
            if attention_pooling == 'avg':
                y_att = torch.mean(y_att, dim=-1)
            elif attention_pooling == 'max':
                y_att = torch.amax(y_att, dim=-1)
            else:
                raise ValueError(
                    "Unrecognized attention pooling strategy '{}'".format(
                        attention_pooling
                    )
                )
    if apply_softmax:
        y_pred = torch.nn.functional.softmax(y_pred, dim=1)
    return y_pred, y_att, y_uncertainty



def _predict_mil(
    model: "torch.nn.Module",
    bags: Union[np.ndarray, List[str]],
    *,
    attention: bool = False,
    attention_pooling: str = 'avg',
    use_lens: bool = False,
    device: Optional[Any] = None,
    apply_softmax: bool = True,
    uq: bool = False
) -> Tuple[np.ndarray, List[np.ndarray]]:
    """Generate MIL predictions for a list of bags."""

    import torch

    attention, uq = _validate_model(model, attention, uq, allow_errors=True)

    y_pred = []
    y_att  = []
    uncertainty = []
    device = utils._detect_device(model, device, verbose=True)

    for bag in bags:
        if utils._is_list_of_paths(bag):
            # If bags are passed as a list of paths, load them individually.
            loaded = torch.cat([utils._load_bag(b).to(device) for b in bag], dim=0)
        else:
            loaded = utils._load_bag(bag).to(device)

        loaded = torch.unsqueeze(loaded, dim=0)
        #Log the shape of loaded
        logging.debug(f"Loaded shape: {loaded.shape}")

        #If slide-level embedding (only one real embedding dimension) [1,1,512] -> [1,512]
        if len(loaded.shape) == 3 and loaded.shape[1] == 1:
            loaded = torch.squeeze(loaded, dim=1)
            logging.debug(f"Fixed loaded shape: {loaded.shape}")

        with torch.no_grad():
            # Run inference.
            _y_pred, _y_att, _y_uq = run_inference(
                model,
                loaded,
                attention=attention,
                attention_pooling=attention_pooling,
                uq=uq,
                apply_softmax=apply_softmax,
                device=device,
                use_lens=use_lens
            )

            # Convert to numpy.
            if _y_pred is not None:
                _y_pred = _y_pred.cpu().numpy()
            if _y_att is not None:
                _y_att = _y_att.cpu().numpy()
            if _y_uq is not None:
                _y_uq = _y_uq.cpu().numpy()

            # Append to running lists.
            y_pred.append(_y_pred)
            if _y_att is not None:
                y_att.append(_y_att)
            if _y_uq is not None:
                uncertainty.append(_y_uq)

    yp = np.concatenate(y_pred, axis=0)
    if uq:
        uncertainty = np.concatenate(uncertainty, axis=0)
        return yp, y_att, uncertainty
    else:
        return yp, y_att


def _predict_mil_tiles(
    model: "torch.nn.Module",
    bag: Union[str, List[str]],
    *,
    use_lens: bool = False,
    device: Optional[Any] = None,
    apply_softmax: bool = True,
    use_first_out: bool = False,
    attention: bool = False,
    attention_pooling: str = 'avg',
    uq: bool = False,
) -> Tuple[np.ndarray, List[np.ndarray]]:
    """Generate tile predictions from an MIL model from a bag."""

    import torch

    attention, uq = _validate_model(model, attention, uq, allow_errors=True)

    # Prepare bag.
    device = utils._detect_device(model, device, verbose=True)
    model.eval()
    if utils._is_list_of_paths(bag):
        # If bags are passed as a list of paths, load them individually.
        loaded = torch.cat([utils._load_bag(b).to(device) for b in bag], dim=0)
    else:
        loaded = utils._load_bag(bag).to(device)

    # Resize the bag dimension to the batch dimension.
    loaded = torch.unsqueeze(loaded, dim=1)

    # Prepare lens.
    if use_lens:
        use_lens = torch.ones(loaded.shape[0]).to(device)

    # Inference.
    with torch.no_grad():
        y_pred, y_att, uncertainty = run_inference(
            model,
            loaded,
            attention=attention,
            attention_pooling=attention_pooling,
            uq=uq,
            use_first_out=use_first_out,
            apply_softmax=apply_softmax,
            use_lens=use_lens,
            device=device
        )

    # Convert to numpy.
    if y_pred is not None:
        y_pred = y_pred.cpu().numpy()
    if y_att is not None:
        y_att = y_att.cpu().numpy()
    if uncertainty is not None:
        uncertainty = uncertainty.cpu().numpy()

    if uq:
        return y_pred, y_att, uncertainty
    else:
        return y_pred, y_att


def _predict_multimodal_mil(
    model: "torch.nn.Module",
    bags: Union[List[np.ndarray], List[List[str]]],
    attention: bool = True,
    use_lens: bool = True,
    device: Optional[Any] = None
) -> Tuple[np.ndarray, List[List[np.ndarray]]]:
    """Generate multi-mag MIL predictions for a nested list of bags."""
    import torch

    y_pred = []
    n_mag = len(bags[0])
    y_att  = [[] for _ in range(n_mag)]
    device = utils._detect_device(model, device, verbose=True)

    # Ensure the model has attention capabilities.
    if attention and not hasattr(model, 'calculate_attention'):
        log.warning(
            "Model '{}' does not have a method 'calculate_attention'. "
            "Unable to calculate or display attention heatmaps.".format(
                model.__class__.__name__
            )
        )
        attention = False

    for bag in bags:
        loaded = [torch.unsqueeze(utils._load_bag(b).to(device), dim=0)
                  for b in bag]
        with torch.no_grad():
            if use_lens:
                model_args = [(mag_bag, torch.from_numpy(np.array([mag_bag.shape[1]])).to(device))
                              for mag_bag in loaded]
            else:
                model_args = (loaded,)
            model_out = model(*model_args)
            if attention:
                raw_att = model.calculate_attention(*model_args)
                for mag in range(n_mag):
                    att = torch.squeeze(raw_att[mag], dim=0)
                    y_att[mag].append(att.cpu().numpy())
            if isinstance(model_out, tuple):
                model_out = model_out[0]
            else:
                model_out = model_out
            y_pred.append(torch.nn.functional.softmax(model_out, dim=1).cpu().numpy())
    yp = np.concatenate(y_pred, axis=0)
    return yp, y_att