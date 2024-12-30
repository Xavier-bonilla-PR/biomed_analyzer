import subprocess
from subprocess import TimeoutExpired
import os
import json
import os.path as osp
from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput
import anthropic
import time
import io
import threading

client = anthropic.Anthropic()

MAX_ITERATIONS = 5
MAX_RUNS = 5
MAX_STDERR_OUTPUT = 1500

coder_prompt = """Your goal is to implement the following idea: {title}.
The proposed experiment is as follows: {idea}.
You are given a total of up to {max_runs} runs to complete the necessary experiments. You do not need to use all {max_runs}.

First, plan the list of experiments you would like to run. For example, if you are sweeping over a specific hyperparameter, plan each value you would like to test for each run.

After you complete each change, we will run the command `python {file_name}.py' and evaluate the results.
YOUR PROPOSED CHANGE MUST USE THIS COMMAND FORMAT, DO NOT ADD ADDITIONAL COMMAND LINE ARGS.
You can then implement the next thing on your list."""

def generate_and_run_code(prompt: str, file_name, fname, experiment_dir, allow_human_input: bool) -> None:
    io = InputOutput(
            yes=True, chat_history_file=osp.join(experiment_dir, f"{file_name}_aider.txt")
        )
    notes = osp.join(experiment_dir, f"{file_name}_notes.txt")
    fname = [osp.join(experiment_dir, f) for f in fname]
    fname.append(notes)
    print(fname)
    model = Model("claude-3-5-sonnet-20240620")
    coder = Coder.create(
        main_model=model,
        io=io,
        fnames=fname,
        stream=False,
        use_git=False,
        edit_format="diff",
    )

    run = 1
    while run < MAX_RUNS + 1:
        print(f"Run {run}")
        
        # Generate code
        generated_code = coder.run(prompt)
        print("Code generated.")
        # Run the generated code
        result = run_experiment(run, file_name, experiment_dir)
        print("Code run.")
        if result.returncode == 0:
            next_prompt = f"""Run {run} completed. Here are the results:
{result.stdout}

Decide if you need to re-plan your experiments given the result (you often will not need to).

Modify file {file_name}_notes.txt and include *all* relevant information for the writeup on Run {run}, including an experiment description and the run number. Be as verbose as necessary.

Then, implement the next thing on your list.
We will then run the command `python {file_name}.py'.
YOUR PROPOSED CHANGE MUST USE THIS COMMAND FORMAT, DO NOT ADD ADDITIONAL COMMAND LINE ARGS.
If you are finished with experiments, respond with 'ALL_COMPLETED'."""
        else:
            stderr_output = result.stderr
            if len(stderr_output) > MAX_STDERR_OUTPUT:
                stderr_output = "..." + stderr_output[-MAX_STDERR_OUTPUT:]
            next_prompt = f"Run failed with the following error {stderr_output}"
        
        if allow_human_input:
            human_input = get_human_input(run)
            if human_input.lower() == 'exit':
                print("Experiment stopped by user.")
                break
            next_prompt += f"\n\nHuman input for run {run}: {human_input}\nPlease consider this input in your next steps."
        
        prompt = next_prompt
        if "ALL_COMPLETED" in generated_code:
            break
        run += 1
    
    if run == MAX_RUNS + 1:
        print("Maximum runs reached. Experiments completed.")


def run_experiment(run_num, file_name, experiment_dir, timeout=35):
    command = [
        "python",
        osp.join(experiment_dir, f"{file_name}.py")]
    
    def read_stream(stream, buffer):
        for line in iter(stream.readline, ''):
            print(f"{'STDOUT' if stream == process.stdout else 'STDERR'}: {line.strip()}")
            buffer.write(line)
        stream.close()

    try:
        print(f"Starting run {run_num} with command: {' '.join(command)}")
        start_time = time.time()
        
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1, universal_newlines=True)
        
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        
        stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, stdout_buffer))
        stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, stderr_buffer))
        
        stdout_thread.start()
        stderr_thread.start()
        
        while True:
            print(f"Time elapsed: {time.time() - start_time:.2f} seconds")  # Debug print
            
            if process.poll() is not None:
                print("Process has terminated")  # Debug print
                break
            
            if time.time() - start_time > timeout:
                print("Timeout reached")  # Debug print
                process.terminate()
                stdout_thread.join()
                stderr_thread.join()
                return subprocess.CompletedProcess(args=command, returncode=1, stdout=stdout_buffer.getvalue(), stderr=f"Timed out after {timeout} seconds")
            
            time.sleep(1)  # Check status every second
        
        stdout_thread.join()
        stderr_thread.join()
        
        returncode = process.returncode
        
        print(f"Run {run_num} completed in {time.time() - start_time:.2f} seconds")
        if returncode != 0:
            print(f"Run {run_num} failed with return code {returncode}")
            print(f"Error output: {stderr_buffer.getvalue()}")
        
        return subprocess.CompletedProcess(args=command, returncode=returncode, stdout=stdout_buffer.getvalue(), stderr=stderr_buffer.getvalue())
    
    except Exception as e:
        print(f"An error occurred: {str(e)}")
        return subprocess.CompletedProcess(args=command, returncode=1, stdout="", stderr=str(e))

def get_human_input(run_num):
    print(f"\nRun {run_num} completed. You can now provide input or suggestions for the next run.")
    print("Enter 'exit' to stop the experiment.")
    return input("Your input: ")

if __name__ == "__main__":
    file_name = "scRNAseq_data_analysis"#input("What is file name? (without '.py') ")
    title = "scRNAseq Data Analyzer"#input("What is title? ")
    idea = "Analyzes scRNAseq data in .h5 file format."#input('What is idea: ')
    fname = []#input("Files as a base? (no commas, only space, include the .py) ").split()
    allow_human_input = input("Allow human input between runs? (yes/no) ").lower() == 'yes'

    # Create a directory for the experiment
    experiment_dir = f"{file_name}_experiment"
    os.makedirs(experiment_dir, exist_ok=True)
    

    # Copy base files to the experiment directory
    for f in fname:
        if os.path.exists(f):
            with open(f, 'r') as source_file:
                content = source_file.read()
            with open(os.path.join(experiment_dir, f), 'w') as dest_file:
                dest_file.write(content)

    initial_prompt = coder_prompt.format(
        title=title,
        idea=idea,
        max_runs=MAX_RUNS,
        file_name = file_name
    )
    generate_and_run_code(initial_prompt, file_name, fname, experiment_dir, allow_human_input)

    #"Automated and Interactive Bioinformatics Analysis Script Generator"
    #"A bioinformatics-focused data analysis assistant that generates customized Python scripts for various genomic and molecular biology datasets, providing basic analysis workflows based on data."
    #["ite_code_gen_2.py", "bioinform_analysis.py"]#