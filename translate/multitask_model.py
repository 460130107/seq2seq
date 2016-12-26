import time
import math
import numpy as np
from translate import utils
from translate.translation_model import TranslationModel, BaseTranslationModel


class MultiTaskModel(BaseTranslationModel):
    def __init__(self, name, tasks, checkpoint_dir, keep_best=1, main_task=None, **kwargs):
        """
        Proxy for several translation models that are trained jointly
        This class presents the same interface as TranslationModel
        """
        self.models = []
        self.ratios = []

        for task in tasks:
            self.checkpoint_dir = checkpoint_dir
            # merging both dictionaries (task parameters have a higher precedence)
            kwargs_ = dict(task)
            for k, v in kwargs.items():
                kwargs_.setdefault(k, v)

            model = TranslationModel(checkpoint_dir=None, keep_best=keep_best, **kwargs_)

            self.models.append(model)
            self.ratios.append(task.ratio if task.ratio is not None else 1)

        self.ratios = [ratio / sum(self.ratios) for ratio in self.ratios]  # unit normalization

        self.main_task = main_task
        self.global_step = 0  # steps of all tasks combined
        super(MultiTaskModel, self).__init__(name, checkpoint_dir, keep_best)

    def train(self, sess, beam_size, steps_per_checkpoint, score_function, steps_per_eval=None, max_train_size=None,
              max_dev_size=None, eval_output=None, max_steps=0, auxiliary_score_function=None, script_dir='scripts',
              read_ahead=10, eval_burn_in=0, decay_if_no_progress=5, **kwargs):
        utils.log('reading training and development data')

        self.global_step = 0
        for model in self.models:
            model.read_data(max_train_size, max_dev_size, read_ahead=read_ahead)
            # those parameters are used to track the progress of each task
            model.loss, model.time, model.steps = 0, 0, 0
            model.previous_losses = []
            global_step = model.global_step.eval(sess)
            for _ in range(global_step):   # read all the data up to this step
                next(model.batch_iterator)

            self.global_step += global_step

        utils.log('starting training')
        while True:
            i = np.random.choice(len(self.models), 1, p=self.ratios)[0]
            model = self.models[i]

            start_time = time.time()
            loss = model.train_step(sess)
            model.loss += loss

            model.time += (time.time() - start_time)
            model.steps += 1
            self.global_step += 1

            if steps_per_checkpoint and self.global_step % steps_per_checkpoint == 0:
                for model_ in self.models:
                    if model_.steps == 0:
                        continue

                    loss_ = model_.loss / model_.steps
                    step_time_ = model_.time / model_.steps

                    utils.log('{} step {} learning rate {:.4f} step-time {:.2f} loss {:.2f}'.format(
                        model_.name, model_.global_step.eval(sess), model_.learning_rate.eval(),
                        step_time_, loss_))
                    
                    if decay_if_no_progress and len(model_.previous_losses) > decay_if_no_progress:
                        if loss_ >= max(model_.previous_losses):
                            sess.run(model_.learning_rate_decay_op)

                    model_.previous_losses.append(loss_)
                    model_.loss, model_.time, model_.steps = 0, 0, 0
                    model_.eval_step(sess)

                self.save(sess)

            if steps_per_eval and self.global_step % steps_per_eval == 0 and 0 <= eval_burn_in <= self.global_step:
                score = 0

                for ratio, model_ in zip(self.ratios, self.models):
                    if eval_output is None:
                        output = None
                    elif len(model_.filenames.dev) > 1:
                        # if there are several dev files, we define several output files
                        # TODO: put dev_prefix into the name of the output file (also in the logging output)
                        output = [
                            '{}.{}.{}.{}'.format(eval_output, i + 1, model_.name, model_.global_step.eval(sess))
                            for i in range(len(model_.filenames.dev))
                        ]
                    else:
                        output = '{}.{}.{}'.format(eval_output, model_.name, model_.global_step.eval(sess))

                    scores_ = model_.evaluate(
                        sess, beam_size, on_dev=True, output=output, score_function=score_function,
                        auxiliary_score_function=auxiliary_score_function, script_dir=script_dir,
                        max_dev_size=max_dev_size
                    )
                    score_ = scores_[0]  # in case there are several dev files, only the first one counts

                    # if there is a main task, pick best checkpoint according to its score
                    # otherwise use the average score across tasks
                    if self.main_task is None:
                        score += ratio * score_
                    elif model_.name == self.main_task:
                        score = score_

                self.manage_best_checkpoints(self.global_step, score)

            if 0 < max_steps <= self.global_step:
                utils.log('finished training')
                return

    def decode(self, *args, **kwargs):
        if self.main_task is not None:
            model = next(model for model in self.models if model.name == self.main_task)
        else:
            model = self.models[0]
        return model.decode(*args, **kwargs)

    def evaluate(self, *args, **kwargs):
        if self.main_task is not None:
            model = next(model for model in self.models if model.name == self.main_task)
        else:
            model = self.models[0]
        return model.evaluate(*args, **kwargs)

    def align(self, *args, **kwargs):
        if self.main_task is not None:
            model = next(model for model in self.models if model.name == self.main_task)
        else:
            model = self.models[0]
        return model.align(*args, **kwargs)
