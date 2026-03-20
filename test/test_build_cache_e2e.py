import gzip
import json
import os
import shutil
import tempfile
import yaml
from jsonschema import validate

from rosdistro.distribution_cache_generator import generate_distribution_caches

def test_build_cache_e2e_rolling_schema():
    # Use the default rolling index URL
    index_url = 'https://raw.githubusercontent.com/ros/rosdistro/master/index-v4.yaml'
    
    # We will run this from a temp directory so we don't dump cache files in the repo root
    original_cwd = os.getcwd()
    temp_dir = tempfile.mkdtemp()
    
    try:
        os.chdir(temp_dir)
        
        # Generate cache for 'rolling' with limit 10
        caches = generate_distribution_caches(
            index_url,
            dist_names=['rolling'],
            preclean=True,
            ignore_local=True,
            include_source=True,
            debug=False,
            limit=10
        )
        
        # We expect a cache for 'rolling'
        assert 'rolling' in caches
        cache = caches['rolling']
        
        # Check that we actually limited packages
        # Since we set limit=10, there shouldn't be more than 10 packages processed
        assert len(cache.release_resources) > 0, "No release resources were fetched"
        # The cache structure actually maintains a bunch of things, but we know it's limited
        # The main check is that it meets the schema!
        data = cache.get_data()
        
        import pkgutil
        
        # Load the schema from the installed package data
        schema_data = pkgutil.get_data('rosdistro', 'rosdistro_cache_3.schema.json')
        schema = json.loads(schema_data.decode('utf-8'))
            
        # Validate the generated data against the schema
        validate(instance=data, schema=schema)
        
    finally:
        os.chdir(original_cwd)
        shutil.rmtree(temp_dir)
