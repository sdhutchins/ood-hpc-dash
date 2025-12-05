![Experimental](https://img.shields.io/badge/stability-experimental-blue.svg)

# Open OnDemand HPC Dashboard

This is a Flask app that serves as a dashboard on the Cheaha HPC.

So far, you can:
- View all modules available by searching and filtering by categories

Other features will include:
- A jobs tab to monitor your current jobs and available resources
- An HTML viewer
- An envs tab that will list existing conda environments and tell how to activate them

## Installation

Create the app directory, clone the repository, and run setup:

```bash
# Create the sandbox apps directory (Open OnDemand scans this location for apps)
mkdir -p /data/user/$USER/ondemand/dev

# Navigate to the sandbox directory
cd /data/user/$USER/ondemand/dev

# Clone the repository (replace <repository-url> with your actual repo URL)
git clone <repository-url> ood-flask-template

# Enter the app directory
cd ood-flask-template

# Run setup to create venv and install dependencies
./setup.sh
```

### Explanation of `setup.sh`

Run `setup.sh` to create a virtual environment and install dependencies. The script also creates `bin/python`, which Passenger uses instead of system Python. This ensures Passenger uses your venv's Python with Flask installed.

## Learn More

- [Open OnDemand Documentation](https://osc.github.io/ood-documentation/latest/)
- [Tutorials for Passenger Apps](https://osc.github.io/ood-documentation/latest/tutorials/tutorials-passenger-apps/)
- [App Development Guide](https://osc.github.io/ood-documentation/latest/how-tos/app-development/)
- [Interactive Apps](https://osc.github.io/ood-documentation/latest/how-tos/app-development/interactive/)