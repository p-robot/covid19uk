"""Represents the analytic pipeline as a ruffus chain"""

import os
import yaml
import pandas as pd
import ruffus as rf


from covid.tasks import (
    assemble_data,
    mcmc,
    thin_posterior,
    next_generation_matrix,
    overall_rt,
    predict,
    summarize,
    within_between,
    case_exceedance,
    summary_geopackage,
    insample_predictive_timeseries,
    summary_longformat,
)

__all__ = ["run_pipeline"]


def _make_append_work_dir(work_dir):
    return lambda filename: os.path.expandvars(os.path.join(work_dir, filename))


def run_pipeline(global_config, results_directory, cli_options):

    wd = _make_append_work_dir(results_directory)

    # Pipeline starts here
    @rf.mkdir(results_directory)
    @rf.originate(wd("config.yaml"), global_config)
    def save_config(output_file, config):
        with open(output_file, "w") as f:
            yaml.dump(config, f)

    @rf.transform(
        save_config,
        rf.formatter(),
        wd("pipeline_data.pkl"),
        global_config,
    )
    def process_data(input_file, output_file, config):
        assemble_data(output_file, config["ProcessData"])

    @rf.transform(
        process_data,
        rf.formatter(),
        wd("posterior.hd5"),
        global_config,
    )
    def run_mcmc(input_file, output_file, config):
        mcmc(input_file, output_file, config["Mcmc"])

    @rf.transform(
        input=run_mcmc,
        filter=rf.formatter(),
        output=wd("thin_samples.pkl"),
        extras=[global_config],
    )
    def thin_samples(input_file, output_file, config):
        thin_posterior(input_file, output_file, config["ThinPosterior"])

    # Rt related steps
    rf.transform(
        input=[[process_data, thin_samples]],
        filter=rf.formatter(),
        output=wd("ngm.pkl"),
    )(next_generation_matrix)

    rf.transform(
        input=next_generation_matrix,
        filter=rf.formatter(),
        output=wd("national_rt.xlsx"),
    )(overall_rt)

    # In-sample prediction
    @rf.transform(
        input=[[process_data, thin_samples]],
        filter=rf.formatter(),
        output=wd("insample7.pkl"),
    )
    def insample7(input_files, output_file):
        predict(
            data=input_files[0],
            posterior_samples=input_files[1],
            output_file=output_file,
            initial_step=-8,
            num_steps=28,
        )

    @rf.transform(
        input=[[process_data, thin_samples]],
        filter=rf.formatter(),
        output=wd("insample14.pkl"),
    )
    def insample14(input_files, output_file):
        return predict(
            data=input_files[0],
            posterior_samples=input_files[1],
            output_file=output_file,
            initial_step=-14,
            num_steps=28,
        )

    # Medium-term prediction
    @rf.transform(
        input=[[process_data, thin_samples]],
        filter=rf.formatter(),
        output=wd("medium_term.pkl"),
    )
    def medium_term(input_files, output_file):
        return predict(
            data=input_files[0],
            posterior_samples=input_files[1],
            output_file=output_file,
            initial_step=-1,
            num_steps=61,
        )

    # Summarisation
    rf.transform(
        input=next_generation_matrix,
        filter=rf.formatter(),
        output=wd("rt_summary.csv"),
    )(summarize.rt)

    rf.transform(
        input=medium_term,
        filter=rf.formatter(),
        output=wd("infec_incidence_summary.csv"),
    )(summarize.infec_incidence)

    rf.transform(
        input=[[process_data, thin_samples, medium_term]],
        filter=rf.formatter(),
        output=wd("prevalence_summary.csv"),
    )(summarize.prevalence)

    rf.transform(
        input=[[process_data, thin_samples]],
        filter=rf.formatter(),
        output=wd("within_between_summary.csv"),
    )(within_between)

    @rf.transform(
        input=[[process_data, insample7, insample14]],
        filter=rf.formatter(),
        output=wd("exceedance_summary.csv"),
    )
    def exceedance(input_files, output_file):
        exceed7 = case_exceedance((input_files[0], input_files[1]), 7)
        exceed14 = case_exceedance((input_files[0], input_files[2]), 14)
        df = pd.DataFrame(
            {"Pr(pred<obs)_7": exceed7, "Pr(pred<obs)_14": exceed14},
            index=exceed7.coords["location"],
        )
        df.to_csv(output_file)

    # Plot in-sample
    @rf.transform(
        input=[insample7, insample14],
        filter=rf.formatter(".+/insample(?P<LAG>\d+).pkl"),
        add_inputs=rf.add_inputs(process_data),
        output="{path[0]}/insample_plots{LAG[0]}",
        extras=["{LAG[0]}"],
    )
    def plot_insample_predictive_timeseries(input_files, output_dir, lag):
        insample_predictive_timeseries(input_files, output_dir, lag)

    # Geopackage
    rf.transform(
        [
            [
                process_data,
                summarize.rt,
                summarize.infec_incidence,
                summarize.prevalence,
                within_between,
                exceedance,
            ]
        ],
        rf.formatter(),
        wd("prediction.gpkg"),
        global_config["Geopackage"],
    )(summary_geopackage)

    rf.cmdline.run(cli_options)

    # DSTL Summary
    rf.transform(
        [[process_data, insample14, medium_term, next_generation_matrix]],
        rf.formatter(),
        wd("summary_longformat.xlsx"),
    )(summary_longformat)

    rf.cmdline.run(cli_options)
