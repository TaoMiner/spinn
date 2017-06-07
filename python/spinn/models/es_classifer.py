"""

NOTE: Not currently usable.

"""


import os
import json
import random
import sys
import time

import gflags
import numpy as np

from spinn.util import afs_safe_logger
from spinn.util.data import SimpleProgressBar
from spinn.util.blocks import get_l2_loss, the_gpu, to_gpu
from spinn.util.misc import Accumulator, EvalReporter
from spinn.util.misc import recursively_set_device
from spinn.util.metrics import MetricsWriter
from spinn.util.logging import train_format, train_extra_format, train_stats, train_accumulate
from spinn.util.logging import train_metrics, eval_metrics
from spinn.util.logging import eval_format, eval_extra_format, eval_stats, eval_accumulate
from spinn.util.loss import auxiliary_loss
from spinn.util.sparks import sparks
import spinn.util.evalb as evalb

# PyTorch
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F
import torch.multiprocessing as mp

from spinn.models.base import get_data_manager, get_flags, get_batch
from spinn.models.base import flag_defaults, init_model
from spinn.models.base import get_checkpoint_path, log_path
from spinn.models.base import load_data_and_embeddings


FLAGS = gflags.FLAGS


def evaluate(FLAGS, model, data_manager, eval_set, index, logger, step, vocabulary=None):
    filename, dataset = eval_set

    A = Accumulator()
    M = MetricsWriter(os.path.join(FLAGS.metrics_path, FLAGS.experiment_name))
    reporter = EvalReporter()

    eval_str = eval_format(model)
    eval_extra_str = eval_extra_format(model)

    # Evaluate
    total_batches = len(dataset)
    progress_bar = SimpleProgressBar(msg="Run Eval", bar_length=60, enabled=FLAGS.show_progress_bar)
    progress_bar.step(0, total=total_batches)
    total_tokens = 0
    start = time.time()

    model.eval()
    for i, dataset_batch in enumerate(dataset):
        batch = get_batch(dataset_batch)
        eval_X_batch, eval_transitions_batch, eval_y_batch, eval_num_transitions_batch, eval_ids = batch

        # Run model.
        output = model(eval_X_batch, eval_transitions_batch, eval_y_batch,
                       use_internal_parser=FLAGS.use_internal_parser,
                       validate_transitions=FLAGS.validate_transitions)

        # Normalize output.
        logits = F.log_softmax(output)

        # Calculate class accuracy.
        target = torch.from_numpy(eval_y_batch).long()
        pred = logits.data.max(1)[1].cpu()  # get the index of the max log-probability

        eval_accumulate(model, data_manager, A, batch)
        A.add('class_correct', pred.eq(target).sum())
        A.add('class_total', target.size(0))

        # Optionally calculate transition loss/acc.
        model.transition_loss if hasattr(model, 'transition_loss') else None

        # Update Aggregate Accuracies
        total_tokens += sum([(nt + 1) / 2 for nt in eval_num_transitions_batch.reshape(-1)])

        if FLAGS.write_eval_report:
            reporter_args = [pred, target, eval_ids, output.data.cpu().numpy()]
            if hasattr(model, 'transition_loss'):
                transitions_per_example, _ = model.spinn.get_transitions_per_example(
                    style="preds" if FLAGS.eval_report_use_preds else "given")
                if model.use_sentence_pair:
                    batch_size = pred.size(0)
                    sent1_transitions = transitions_per_example[:batch_size]
                    sent2_transitions = transitions_per_example[batch_size:]
                    reporter_args.append(sent1_transitions)
                    reporter_args.append(sent2_transitions)
                else:
                    reporter_args.append(transitions_per_example)
            reporter.save_batch(*reporter_args)

        # Print Progress
        progress_bar.step(i + 1, total=total_batches)
    progress_bar.finish()

    end = time.time()
    total_time = end - start

    A.add('total_tokens', total_tokens)
    A.add('total_time', total_time)

    stats_args = eval_stats(model, A, step)
    stats_args['filename'] = filename

    logger.Log(eval_str.format(**stats_args))
    logger.Log(eval_extra_str.format(**stats_args))

    if FLAGS.write_eval_report:
        eval_report_path = os.path.join(FLAGS.log_path, FLAGS.experiment_name + ".report")
        reporter.write_report(eval_report_path)

    eval_class_acc = stats_args['class_acc']
    eval_trans_acc = stats_args['transition_acc']

    if index == 0:
        eval_metrics(M, stats_args, step)

    return eval_class_acc, eval_trans_acc


def train_loop(FLAGS, data_manager, model, optimizer, trainer,
               training_data_iter, eval_iterators, logger, step, best_dev_error, perturbation_id):
    perturbation_name = FLAGS.experiment_name + perturbation_id
    # Accumulate useful statistics.
    A = Accumulator(maxlen=FLAGS.deque_length)
    M = MetricsWriter(os.path.join(FLAGS.metrics_path, perturbation_name))

    # Checkpoint paths.
    standard_checkpoint_path = get_checkpoint_path(FLAGS.ckpt_path, perturbation_name)
    best_checkpoint_path = get_checkpoint_path(FLAGS.ckpt_path, perturbation_name, best=True)

    # Load checkpoint if available.
    if FLAGS.load_best and os.path.isfile(best_checkpoint_path):
        logger.Log("Found best checkpoint, restoring.")
        step, best_dev_error = trainer.load(best_checkpoint_path)
        logger.Log(
            "Resuming at step: {} with best dev accuracy: {}".format(
                step, 1. - best_dev_error))
    else os.path.isfile(standard_checkpoint_path):
        logger.Log("Found checkpoint, restoring.")
        step, best_dev_error = trainer.load(standard_checkpoint_path)
        logger.Log(
            "Resuming at step: {} with best dev accuracy: {}".format(
                step, 1. - best_dev_error))
        step = 0
        best_dev_error = 1.0

    # Build log format strings.
    model.train()
    X_batch, transitions_batch, y_batch, num_transitions_batch, train_ids = get_batch(
        training_data_iter.next())
    if not FLAGS.transition_detach:
        logger.Log("Transition decision function is going to be detached.")
        ## SWITCH TO EVOLUTION FLAG 
    model(X_batch, transitions_batch, y_batch,
          use_internal_parser=FLAGS.use_internal_parser,
          validate_transitions=FLAGS.validate_transitions,
          transition_detach=True)

    # Train.
    logger.Log("Training perturbation %s" % perturbation_id)

    # New Training Loop
    progress_bar = SimpleProgressBar(msg="Training", bar_length=60, enabled=FLAGS.show_progress_bar)
    progress_bar.step(i=0, total=FLAGS.statistics_interval_steps)
    log_entry = pb.SpinnEntry()

    if not FLAGS.evolution:
        num_steps = FLAGS.training_steps
    else:
        num_steps = 1000 #hard coded

    for step in range(step, num_steps):
        model.train()

        start = time.time()

        batch = get_batch(training_data_iter.next())
        X_batch, transitions_batch, y_batch, num_transitions_batch, train_ids = batch

        total_tokens = sum([(nt + 1) / 2 for nt in num_transitions_batch.reshape(-1)])

        # Reset cached gradients.
        optimizer.zero_grad()

        # Run model.
        output = model(X_batch, transitions_batch, y_batch,
                       use_internal_parser=FLAGS.use_internal_parser,
                       validate_transitions=FLAGS.validate_transitions
                       )

        # Normalize output.
        logits = F.log_softmax(output)

        # Calculate class accuracy.
        target = torch.from_numpy(y_batch).long()
        pred = logits.data.max(1)[1].cpu()  # get the index of the max log-probability
        class_acc = pred.eq(target).sum() / float(target.size(0))

        # Calculate class loss.
        xent_loss = nn.NLLLoss()(logits, to_gpu(Variable(target, volatile=False)))

        # Optionally calculate transition loss.
        transition_loss = model.transition_loss if hasattr(model, 'transition_loss') else None

        # Extract L2 Cost
        l2_loss = get_l2_loss(model, FLAGS.l2_lambda) if FLAGS.use_l2_loss else None

        # Accumulate Total Loss Variable
        total_loss = 0.0
        total_loss += xent_loss
        if l2_loss is not None:
            total_loss += l2_loss
        if transition_loss is not None and model.optimize_transition_loss:
            total_loss += transition_loss
        aux_loss = auxiliary_loss(model)
        total_loss += aux_loss
        # Backward pass.
        total_loss.backward()

        # Hard Gradient Clipping
        clip = FLAGS.clipping_max_value
        for p in model.parameters():
            if p.requires_grad:
                p.grad.data.clamp_(min=-clip, max=clip)

        # Learning Rate Decay
        if FLAGS.actively_decay_learning_rate:
            optimizer.lr = FLAGS.learning_rate * \
                (FLAGS.learning_rate_decay_per_10k_steps ** (step / 10000.0))

        # Gradient descent step.
        optimizer.step()

        end = time.time()

        total_time = end - start

        train_accumulate(model, data_manager, A, batch)
        A.add('class_acc', class_acc)
        A.add('total_tokens', total_tokens)
        A.add('total_time', total_time)

        if step % FLAGS.statistics_interval_steps == 0:
            progress_bar.step(i=FLAGS.statistics_interval_steps,
                              total=FLAGS.statistics_interval_steps)
            progress_bar.finish()

            A.add('total_loss', total_loss.data[0])
            A.add('auxiliary_loss', aux_loss.data[0])
            A.add('xent_loss', xent_loss.data[0])
            A.add('l2_loss', l2_loss.data[0])
            stats_args = train_stats(model, optimizer, A, step)

            train_metrics(M, stats_args, step)

            logger.Log(train_str.format(**stats_args))
            logger.Log(train_extra_str.format(**stats_args))

        if step % FLAGS.sample_interval_steps == 0 and FLAGS.num_samples > 0:
            model.train()
            model(X_batch, transitions_batch, y_batch,
                  use_internal_parser=FLAGS.use_internal_parser,
                  validate_transitions=FLAGS.validate_transitions
                  )
            tr_transitions_per_example, tr_strength = model.spinn.get_transitions_per_example()

            model.eval()
            model(X_batch, transitions_batch, y_batch,
                  use_internal_parser=FLAGS.use_internal_parser,
                  validate_transitions=FLAGS.validate_transitions
                  )
            ev_transitions_per_example, ev_strength = model.spinn.get_transitions_per_example()

            transition_str = "Samples:"
            if model.use_sentence_pair and len(transitions_batch.shape) == 3:
                transitions_batch = np.concatenate([
                    transitions_batch[:, :, 0], transitions_batch[:, :, 1]], axis=0)

            # This could be done prior to running the batch for a tiny speed boost.
            t_idxs = range(FLAGS.num_samples)
            random.shuffle(t_idxs)
            t_idxs = sorted(t_idxs[:FLAGS.num_samples])
            for t_idx in t_idxs:
                gold = transitions_batch[t_idx]
                pred_tr = tr_transitions_per_example[t_idx]
                pred_ev = ev_transitions_per_example[t_idx]
                stength_tr = sparks([1] + tr_strength[t_idx].tolist())
                stength_ev = sparks([1] + ev_strength[t_idx].tolist())
                _, crossing = evalb.crossing(gold, pred_ev)
                transition_str += "\n{}. crossing(pe)={}".format(t_idx, crossing)
                transition_str += "\n     g{}".format("".join(map(str, gold)))
                transition_str += "\n      {}".format(stength_tr[1:].encode('utf-8'))
                transition_str += "\n    pt{}".format("".join(map(str, pred_tr)))
                transition_str += "\n      {}".format(stength_ev[1:].encode('utf-8'))
                transition_str += "\n    pe{}".format("".join(map(str, pred_ev)))
            logger.Log(transition_str)

        if step > 0 and step % FLAGS.eval_interval_steps == 0:
            for index, eval_set in enumerate(eval_iterators):
                acc, tacc = evaluate(FLAGS, model, data_manager, eval_set, index, logger, step)
                if FLAGS.ckpt_on_best_dev_error and index == 0 and (
                        1 - acc) < 0.99 * best_dev_error and step > FLAGS.ckpt_step:
                    best_dev_error = 1 - acc
                    logger.Log("Checkpointing with new best dev accuracy of %f" % acc)
                    trainer.save(best_checkpoint_path, step, best_dev_error)
            progress_bar.reset()

        if step > FLAGS.ckpt_step and step % FLAGS.ckpt_interval_steps == 0:
            logger.Log("Checkpointing.")
            trainer.save(standard_checkpoint_path, step, best_dev_error)

        progress_bar.step(i=step % FLAGS.statistics_interval_steps,
                          total=FLAGS.statistics_interval_steps)



def rollout(queue, perturbed_model, perturbation_id):
    """
    train for episode length
    """
    #train_loop(FLAGS, data_manager, perturbed_model, optimizer, trainer, training_data_iter, eval_iterators, logger, step, best_dev_error)
    #evaluate(FLAGS, perturbed_model, data_manager, eval_set, index, logger, step)
    queue.put(train_loop(FLAGS, data_manager, perturbed_model, optimizer, 
        trainer, training_data_iter, eval_iterators, logger, step, best_dev_error, perturbation_id))

def perturb_model(self, model, random_seed):
    models = []
    np.random.seed(random_seed)
    for i in range(4):
        # hard coded number of models per cycle
        pert_model = model
        pert_model.load_state_dict(model.state_dict())
        for (k, v) in zip(pert_model.spinn.evolution_params()):
            epsilon = np.random.normal(0, 1, v.size())
            v += torch.from_numpy(FLAGS.es_sigma * epsilon).float()
        models.append()

    """
    pos_model.load_state_dict(model.state_dict())
    neg_model.load_state_dict(model.state_dict())

    np.random.seed(random_seed)

    for (k, v), (_k, _v) in zip(pos_model.spinn.evolution_params(), neg_model.evolution_params()):
        epsilon = np.random.normal(0, 1, v.size())
        v += torch.from_numpy(FLAGS.es_sigma * epsilon).float()
        _v += torch.from_numpy(FLAGS.es_sigma * (-epsilon)).float()
    return [pos_model, neg_model]
    """
    return models

def generate_seeds_and_models(chosen_model):
    np.random.seed()
    random_seed = np.random.randint(2**20)
    models = perturb_model(chosen_model, random_seed)
    return random_seed, models

def run(only_forward=False):
    logger = afs_safe_logger.Logger(log_path(FLAGS))

    data_manager = get_data_manager(FLAGS.data_type)

    logger.Log("Flag Values:\n" + json.dumps(FLAGS.FlagValuesDict(), indent=4, sort_keys=True))

    # Get Data and Embeddings
    vocabulary, initial_embeddings, training_data_iter, eval_iterators = \
        load_data_and_embeddings(FLAGS, data_manager, logger,
                                 FLAGS.training_data_path, FLAGS.eval_data_path)

    # Build model.
    vocab_size = len(vocabulary)
    num_classes = len(data_manager.LABEL_MAP)

    model, optimizer, trainer = init_model(
        FLAGS, logger, initial_embeddings, vocab_size, num_classes, data_manager)

    """
    standard_checkpoint_path = get_checkpoint_path(FLAGS.ckpt_path, FLAGS.experiment_name)
    best_checkpoint_path = get_checkpoint_path(FLAGS.ckpt_path, FLAGS.experiment_name, best=True)

    # Load checkpoint if available.
    if FLAGS.load_best and os.path.isfile(best_checkpoint_path):
        logger.Log("Found best checkpoint, restoring.")
        step, best_dev_error = trainer.load(best_checkpoint_path)
        logger.Log(
            "Resuming at step: {} with best dev accuracy: {}".format(
                step, 1. - best_dev_error))
    elif os.path.isfile(standard_checkpoint_path):
        logger.Log("Found checkpoint, restoring.")
        step, best_dev_error = trainer.load(standard_checkpoint_path)
        logger.Log(
            "Resuming at step: {} with best dev accuracy: {}".format(
                step, 1. - best_dev_error))
    else:
        assert not only_forward, "Can't run an eval-only run without a checkpoint. Supply a checkpoint."
        step = 0
        best_dev_error = 1.0
    """

    # GPU support.
    the_gpu.gpu = FLAGS.gpu
    if FLAGS.gpu >= 0:
        model.cuda()
    else:
        model.cpu()
    recursively_set_device(optimizer.state_dict(), FLAGS.gpu)

    # Debug
    def set_debug(self):
        self.debug = FLAGS.debug
    model.apply(set_debug)

    # Do an evaluation-only run.
    if only_forward:
        eval_str = eval_format(model)
        logger.Log("Eval-Format: {}".format(eval_str))
        eval_extra_str = eval_extra_format(model)
        logger.Log("Eval-Extra-Format: {}".format(eval_extra_str))

        for index, eval_set in enumerate(eval_iterators):
            evaluate(FLAGS, model, data_manager, eval_set, index, logger, step, vocabulary)

    elif FLAGS.evolution:
        chosen_models = [model]
        num_eps = 0
        total_num_frames = 0
        for ev_step in range(1000):
            # hard coded number of evolution iterations to perform
            if ev_step > 0:
                chosen_models = []
                by_acc = []
                for model in all_models:
                    acc, tacc = evaluate(FLAGS, model, data_manager, eval_set, index, logger, step)
                    by_acc.append((model, acc))
                    # FINISH ME
            processes = []
            queue = mp.Queue()
            all_seeds, all_models = [], []
                for chosen_model in chosen_models:
                    random_seed, models = generate_seeds_and_models(chosen_model)
                    for i in range(len(models)):
                        all_seeds.append(random_seed)
                    all_models += models
                assert len(all_seeds) == len(all_models)

                #while all_models:
                for i in range(len(all_models))
                    #perturbed_model = all_models.pop()
                    #seed = all_seeds.pop()
                    perturbed_model = all_models[i]
                    seed = all_seeds[i]
                    p = mp.Process(target=rollout, args=(queue, perturbed_model, i))
                    p.start()
                    processes.append(p)
                assert len(all_seeds) == 0

    else:
        raise Exception("Please use a different classifier, this one specifically uses Evolution Strategy to train the parser.")

if __name__ == '__main__':
    get_flags()

    # Parse command line flags.
    FLAGS(sys.argv)

    flag_defaults(FLAGS)

    if FLAGS.model_type == "RLSPINN":
        raise Exception(
            "Please use rl_classifier.py instead of supervised_classifier.py for RLSPINN.")

    run(only_forward=FLAGS.expanded_eval_only_mode)