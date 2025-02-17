from typing import Any, Dict, List, Optional, Tuple, TYPE_CHECKING
# from typing import cast as typecast

import json
import logging
import os
import yaml

from .config import Config
from .acresource import ACResource

from ..utils import parse_yaml, dump_yaml

AnyDict = Dict[str, Any]
HandlerResult = Optional[Tuple[str, List[AnyDict]]]

# Some thoughts:
# - loading a bunch of Ambassador resources is different from loading a bunch of K8s
#   services, because we should assume that if we're being a fed a bunch of Ambassador
#   resources, we'll get a full set. The whole 'secret loader' thing needs to have the
#   concept of a TLSSecret resource that can be force-fed to us, or that can be fetched
#   through the loader if needed.
# - If you're running a debug-loop Ambassador, you should just have a flat (or
#   recursive, I don't care) directory full of Ambassador YAML, including TLSSecrets
#   and Endpoints and whatnot, as needed. All of it will get read by
#   load_from_filesystem and end up in the elements array.
# - If you're running expecting to be fed by kubewatch, at present kubewatch will
#   send over K8s Service records, and anything annotated in there will end up in
#   elements. This may include TLSSecrets or Endpoints. Any TLSSecret mentioned that
#   isn't already in elements will need to be fetched.
# - Ambassador resources do not have namespaces. They have the ambassador_id. That's
#   it. The ambassador_id is completely orthogonal to the namespace. No element with
#   the wrong ambassador_id will end up in elements. It would be nice if they were
#   never sent by kubewatch, but, well, y'know.
# - TLSSecret resources are not TLSContexts. TLSSecrets only have a name, a private
#   half, and a public half. They do _not_ have other TLSContext information.
# - Endpoint resources probably have just a name, a service name, and an endpoint
#   address.

class ResourceFetcher:
    def __init__(self, logger: logging.Logger, aconf: 'Config') -> None:
        self.aconf = aconf
        self.logger = logger
        self.elements: List[ACResource] = []
        self.filename: Optional[str] = None
        self.ocount: int = 1
        self.saved: List[Tuple[Optional[str], int]] = []

        self.k8s_endpoints: Dict[str, AnyDict] = {}
        self.k8s_services: Dict[str, AnyDict] = {}
        self.services: Dict[str, AnyDict] = {}

    @property
    def location(self):
        return "%s.%d" % (self.filename or "anonymous YAML", self.ocount)

    def push_location(self, filename: Optional[str], ocount: int) -> None:
        self.saved.append((self.filename, self.ocount))
        self.filename = filename
        self.ocount = ocount

    def pop_location(self) -> None:
        self.filename, self.ocount = self.saved.pop()

    def load_from_filesystem(self, config_dir_path, recurse: bool=False,
                             k8s: bool=False, finalize: bool=True):
        inputs: List[Tuple[str, str]] = []

        if os.path.isdir(config_dir_path):
            dirs = [ config_dir_path ]

            while dirs:
                dirpath = dirs.pop(0)

                for filename in os.listdir(dirpath):
                    filepath = os.path.join(dirpath, filename)

                    if recurse and os.path.isdir(filepath):
                        # self.logger.debug("%s: RECURSE" % filepath)
                        dirs.append(filepath)
                        continue

                    if not os.path.isfile(filepath):
                        # self.logger.debug("%s: SKIP non-file" % filepath)
                        continue

                    if not filename.lower().endswith('.yaml'):
                        # self.logger.debug("%s: SKIP non-YAML" % filepath)
                        continue

                    # self.logger.debug("%s: SAVE configuration file" % filepath)
                    inputs.append((filepath, filename))

        else:
            # this allows a file to be passed into the ambassador cli
            # rather than just a directory
            inputs.append((config_dir_path, os.path.basename(config_dir_path)))

        for filepath, filename in inputs:
            self.logger.info("reading %s (%s)" % (filename, filepath))

            try:
                serialization = open(filepath, "r").read()
                self.parse_yaml(serialization, k8s=k8s, filename=filename, finalize=False)
            except IOError as e:
                self.aconf.post_error("could not read YAML from %s: %s" % (filepath, e))

        if finalize:
            self.finalize()

    def parse_yaml(self, serialization: str, k8s=False, rkey: Optional[str]=None,
                   filename: Optional[str]=None, finalize: bool=True) -> None:
        # self.logger.debug("%s: parsing %d byte%s of YAML:\n%s" %
        #                   (self.location, len(serialization), "" if (len(serialization) == 1) else "s",
        #                    serialization))

        try:
            objects = parse_yaml(serialization)
            self.parse_object(objects=objects, k8s=k8s, rkey=rkey, filename=filename)
        except yaml.error.YAMLError as e:
            self.aconf.post_error("%s: could not parse YAML: %s" % (self.location, e))

        if finalize:
            self.finalize()

    def parse_json(self, serialization: str, k8s=False, rkey: Optional[str]=None,
                   filename: Optional[str]=None, finalize: bool=True) -> None:
        # self.logger.debug("%s: parsing %d byte%s of YAML:\n%s" %
        #                   (self.location, len(serialization), "" if (len(serialization) == 1) else "s",
        #                    serialization))

        try:
            objects = json.loads(serialization)
            self.parse_object(objects=objects, k8s=k8s, rkey=rkey, filename=filename)
        except json.decoder.JSONDecodeError as e:
            self.aconf.post_error("%s: could not parse YAML: %s" % (self.location, e))

        if finalize:
            self.finalize()

    def parse_watt(self, serialization: str, finalize: bool=True) -> None:
        basedir = os.environ.get('AMBASSADOR_CONFIG_BASE_DIR', '/ambassador')

        if os.path.isfile(os.path.join(basedir, '.ambassador_ignore_crds')):
            self.aconf.post_error("Ambassador could not find core CRD definitions. Please visit https://www.getambassador.io/reference/core/crds/ for more information. You can continue using Ambassador via Kubernetes annotations, any configuration via CRDs will be ignored...")

        if os.path.isfile(os.path.join(basedir, '.ambassador_ignore_crds_2')):
            self.aconf.post_error("Ambassador could not find Resolver type CRD definitions. Please visit https://www.getambassador.io/reference/core/crds/ for more information. You can continue using Ambassador via Kubernetes annotations, any configuration via CRDs will be ignored...")

        try:
            watt_dict = json.loads(serialization)

            watt_k8s = watt_dict.get('Kubernetes', {})

            # Handle normal Kube objects...
            for key in [ 'service', 'endpoints', 'secret' ]:
                for obj in watt_k8s.get(key) or []:
                    self.handle_k8s(obj)

            # ...then handle Ambassador CRDs.
            for key in [ 'AuthService', 'ConsulResolver',
                         'KubernetesEndpointResolver', 'KubernetesServiceResolver',
                         'Mapping', 'Module', 'RateLimitService',
                         'TCPMapping', 'TLSContext', 'TracingService',
                         'ClusterIngress']:
                for obj in watt_k8s.get(key) or []:
                    self.handle_k8s_crd(obj)

            watt_consul = watt_dict.get('Consul', {})
            consul_endpoints = watt_consul.get('Endpoints', {})

            for consul_rkey, consul_object in consul_endpoints.items():
                result = self.handle_consul_service(consul_rkey, consul_object)

                if result:
                    rkey, parsed_objects = result

                    self.parse_object(parsed_objects, k8s=False,
                                      filename=self.filename, rkey=rkey)
        except json.decoder.JSONDecodeError as e:
            self.aconf.post_error("%s: could not parse WATT: %s" % (self.location, e))

        if finalize:
            self.finalize()

    def handle_k8s(self, obj: dict) -> None:
        # self.logger.debug("handle_k8s obj %s" % json.dumps(obj, indent=4, sort_keys=True))

        kind = obj.get('kind')

        if not kind:
            # self.logger.debug("%s: ignoring K8s object, no kind" % self.location)
            return

        handler_name = f'handle_k8s_{kind.lower()}'
        handler = getattr(self, handler_name, None)

        if not handler:
            # self.logger.debug("%s: ignoring K8s object, no kind" % self.location)
            return

        result = handler(obj)

        if result:
            rkey, parsed_objects = result

            self.parse_object(parsed_objects, k8s=False,
                              filename=self.filename, rkey=rkey)

    def handle_k8s_crd(self, obj: dict) -> None:
        # CRDs are _not_ allowed to have embedded objects in annotations, because ew.

        kind = obj.get('kind')

        if not kind:
            self.logger.debug("%s: ignoring K8s CRD, no kind" % self.location)
            return

        apiVersion = obj.get('apiVersion')
        metadata = obj.get('metadata') or {}
        name = metadata.get('name')
        namespace = metadata.get('namespace') or 'default'
        spec = obj.get('spec') or {}

        if not name:
            self.logger.debug(f'{self.location}: ignoring K8s {kind} CRD, no name')
            return

        if not apiVersion:
            self.logger.debug(f'{self.location}: ignoring K8s {kind} CRD {name}: no apiVersion')
            return

        # if not spec:
        #     self.logger.debug(f'{self.location}: ignoring K8s {kind} CRD {name}: no spec')
        #     return

        # We use this resource identifier as a key into self.k8s_services, and of course for logging .
        resource_identifier = f'{name}.{namespace}'

        # OK. Shallow copy 'spec'...
        amb_object = dict(spec)

        # ...and then stuff in a couple of other things.
        amb_object['apiVersion'] = apiVersion
        amb_object['name'] = name
        amb_object['kind'] = kind

        # Done. Parse it.
        self.parse_object([ amb_object ], k8s=False, filename=self.filename, rkey=resource_identifier)

    def parse_object(self, objects, k8s=False, rkey: Optional[str]=None, filename: Optional[str]=None):
        self.push_location(filename, 1)

        # self.logger.debug("PARSE_OBJECT: incoming %d" % len(objects))

        for obj in objects:
            self.logger.debug("PARSE_OBJECT: checking %s" % obj)

            if k8s:
                self.handle_k8s(obj)
            else:
                # if not obj:
                #     self.logger.debug("%s: empty object from %s" % (self.location, serialization))

                self.process_object(obj, rkey=rkey)
                self.ocount += 1

        self.pop_location()

    def process_object(self, obj: dict, rkey: Optional[str]=None) -> None:
        if not isinstance(obj, dict):
            # Bug!!
            if not obj:
                self.aconf.post_error("%s is empty" % self.location)
            else:
                self.aconf.post_error("%s is not a dictionary? %s" %
                                      (self.location, json.dumps(obj, indent=4, sort_keys=4)))
            return

        if not self.aconf.good_ambassador_id(obj):
            self.logger.debug("%s ignoring object with mismatched ambassador_id" % self.location)
            return

        if 'kind' not in obj:
            # Bug!!
            self.aconf.post_error("%s is missing 'kind'?? %s" %
                                  (self.location, json.dumps(obj, indent=4, sort_keys=True)))
            return

        # self.logger.debug("%s PROCESS %s initial rkey %s" % (self.location, obj['kind'], rkey))

        # Is this a pragma object?
        if obj['kind'] == 'Pragma':
            # Why did I think this was a good idea? [ :) ]
            new_source = obj.get('source', None)

            if new_source:
                # We don't save the old self.filename here, so this change will last until
                # the next input source (or the next Pragma).
                self.filename = new_source

            # Don't count Pragma objects, since the user generally doesn't write them.
            self.ocount -= 1
            return

        if not rkey:
            rkey = self.filename

        rkey = "%s.%d" % (rkey, self.ocount)

        # self.logger.debug("%s PROCESS %s updated rkey to %s" % (self.location, obj['kind'], rkey))

        # Brutal hackery.
        if obj['kind'] == 'Service':
            self.logger.debug("%s PROCESS saving service %s" % (self.location, obj['name']))
            self.services[obj['name']] = obj
        else:
            # Fine. Fine fine fine.
            serialization = dump_yaml(obj, default_flow_style=False)

            try:
                r = ACResource.from_dict(rkey, rkey, serialization, obj)
                self.elements.append(r)
            except Exception as e:
                self.aconf.post_error(e.args[0])

            self.logger.debug("%s PROCESS %s save %s: %s" % (self.location, obj['kind'], rkey, serialization))

    def sorted(self, key=lambda x: x.rkey):  # returns an iterator, probably
        return sorted(self.elements, key=key)

    def handle_k8s_endpoints(self, k8s_object: AnyDict) -> HandlerResult:
        # Don't include Endpoints unless endpoint routing is enabled.
        if not Config.enable_endpoints:
            return None

        metadata = k8s_object.get('metadata', None)
        resource_name = metadata.get('name') if metadata else None
        resource_namespace = metadata.get('namespace', 'default') if metadata else None
        resource_subsets = k8s_object.get('subsets', None)

        skip = False

        if not metadata:
            self.logger.debug("ignoring K8s Endpoints with no metadata")
            skip = True

        if not resource_name:
            self.logger.debug("ignoring K8s Endpoints with no name")
            skip = True

        if not resource_subsets:
            self.logger.debug(f"ignoring K8s Endpoints {resource_name}.{resource_namespace} with no subsets")
            skip = True

        if skip:
            return None

        # We use this resource identifier as a key into self.k8s_services, and of course for logging .
        resource_identifier = '{name}.{namespace}'.format(namespace=resource_namespace, name=resource_name)

        # K8s Endpoints resources are _stupid_ in that they give you a vector of
        # IP addresses and a vector of ports, and you have to assume that every
        # IP address listens on every port, and that the semantics of each port
        # are identical. The first is usually a good assumption. The second is not:
        # people routinely list 80 and 443 for the same service, for example,
        # despite the fact that one is HTTP and the other is HTTPS.
        #
        # By the time the ResourceFetcher is done, we want to be working with
        # Ambassador Service resources, which have an array of address:port entries
        # for endpoints. So we're going to extract the address and port numbers
        # as arrays of tuples and stash them for later.
        #
        # In Kubernetes-speak, the Endpoints resource has some metadata and a set
        # of "subsets" (though I've personally never seen more than one subset in
        # one of these things).

        for subset in resource_subsets:
            # K8s subset addresses have some node info in with the IP address.
            # May as well save that too.

            addresses = []

            for address in subset.get('addresses', []):
                addr = {}

                ip = address.get('ip', None)
                if ip is not None:
                    addr['ip'] = ip

                node = address.get('nodeName', None)
                if node is not None:
                    addr['node'] = node

                target_ref = address.get('targetRef', None)
                if target_ref is not None:
                    target_kind = target_ref.get('kind', None)
                    if target_kind is not None:
                        addr['target_kind'] = target_kind

                    target_name = target_ref.get('name', None)
                    if target_name is not None:
                        addr['target_name'] = target_name

                    target_namespace = target_ref.get('namespace', None)
                    if target_namespace is not None:
                        addr['target_namespace'] = target_namespace

                if len(addr) > 0:
                    addresses.append(addr)

            # If we got no addresses, there's no point in messing with ports.
            if len(addresses) == 0:
                continue

            ports = subset.get('ports', [])

            # A service can reference a port either by name or by port number.
            port_dict = {}

            for port in ports:
                port_name = port.get('name', None)
                port_number = port.get('port', None)
                port_proto = port.get('protocol', 'TCP').upper()

                if port_proto != 'TCP':
                    continue

                if port_number is None:
                    # WTFO.
                    continue

                port_dict[str(port_number)] = port_number

                if port_name:
                    port_dict[port_name] = port_number

            if port_dict:
                # We're not going to actually return this: we'll just stash it for our
                # later resolution pass.

                self.k8s_endpoints[resource_identifier] = {
                    'name': resource_name,
                    'namespace': resource_namespace,
                    'addresses': addresses,
                    'ports': port_dict
                }
            else:
                self.logger.debug(f"ignoring K8s Endpoints {resource_identifier} with no routable ports")

        return None

    def handle_k8s_service(self, k8s_object: AnyDict) -> HandlerResult:
        # The annoying bit about K8s Service resources is that not only do we have to look
        # inside them for Ambassador resources, but we also have to save their info for
        # later endpoint resolution too.
        #
        # Again, we're trusting that the input isn't overly bloated on that latter bit.

        metadata = k8s_object.get('metadata', None)
        resource_name = metadata.get('name') if metadata else None
        resource_namespace = metadata.get('namespace', 'default') if metadata else None

        annotations = metadata.get('annotations', None) if metadata else None
        if annotations:
            annotations = annotations.get('getambassador.io/config', None)

        skip = False

        if not metadata:
            self.logger.debug("ignoring K8s Service with no metadata")
            skip = True

        if not skip and not resource_name:
            self.logger.debug("ignoring K8s Service with no name")
            skip = True

        if not skip and (Config.single_namespace and (resource_namespace != Config.ambassador_namespace)):
            # This should never happen in actual usage, since we shouldn't be given things
            # in the wrong namespace. However, in development, this can happen a lot.
            self.logger.debug(f"ignoring K8s Service {resource_name}.{resource_namespace} in wrong namespace")
            skip = True

        if skip:
            return None

        # We use this resource identifier as a key into self.k8s_services, and of course for logging .
        resource_identifier = f'{resource_name}.{resource_namespace}'

        # Not skipping. First, if we have some actual ports, stash this in self.k8s_services
        # for later resolution.

        spec = k8s_object.get('spec', None)
        ports = spec.get('ports', None) if spec else None

        if spec and ports:
            self.k8s_services[resource_identifier] = {
                'name': resource_name,
                'namespace': resource_namespace,
                'ports': ports
            }
        else:
            self.logger.debug(f"not saving K8s Service {resource_name}.{resource_namespace} with no ports")

        objects: List[Any] = []

        if annotations:
            if (self.filename is not None) and (not self.filename.endswith(":annotation")):
                self.filename += ":annotation"

            try:
                objects = parse_yaml(annotations)
            except yaml.error.YAMLError as e:
                self.logger.debug("could not parse YAML: %s" % e)

        return resource_identifier, objects

    # Handler for K8s Secret resources.
    def handle_k8s_secret(self, k8s_object: AnyDict) -> HandlerResult:
        # XXX Another one where we shouldn't be saving everything.

        secret_type = k8s_object.get('type', None)
        metadata = k8s_object.get('metadata', None)
        resource_name = metadata.get('name') if metadata else None
        resource_namespace = metadata.get('namespace', 'default') if metadata else None
        data = k8s_object.get('data', None)

        skip = False

        if (secret_type != 'kubernetes.io/tls') and (secret_type != 'Opaque'):
            self.logger.debug("ignoring K8s Secret with unknown type %s" % secret_type)
            skip = True

        if not data:
            self.logger.debug("ignoring K8s Secret with no data")
            skip = True

        if not metadata:
            self.logger.debug("ignoring K8s Secret with no metadata")
            skip = True

        if not resource_name:
            self.logger.debug("ignoring K8s Secret with no name")
            skip = True

        if not skip and (Config.single_namespace and (resource_namespace != Config.ambassador_namespace)):
            # This should never happen in actual usage, since we shouldn't be given things
            # in the wrong namespace. However, in development, this can happen a lot.
            self.logger.debug("ignoring K8s Secret in wrong namespace")
            skip = True

        if skip:
            return None

        # This resource identifier is useful for log output since filenames can be duplicated (multiple subdirectories)
        resource_identifier = f'{resource_name}.{resource_namespace}'

        tls_crt = data.get('tls.crt', None)
        tls_key = data.get('tls.key', None)

        if not tls_crt and not tls_key:
            # Uh. WTFO?
            self.logger.debug(f'ignoring K8s Secret {resource_identifier} with no keys')
            return None

        # No need to muck about with resolution later, just immediately turn this
        # into an Ambassador Secret resource.
        secret_info = {
            'apiVersion': 'ambassador/v1',
            'ambassador_id': Config.ambassador_id,
            'kind': 'Secret',
            'name': resource_name,
            'namespace': resource_namespace
        }

        if tls_crt:
            secret_info['tls_crt'] = tls_crt

        if tls_key:
            secret_info['tls_key'] = tls_key

        return resource_identifier, [ secret_info ]

    # Handler for Consul services
    def handle_consul_service(self,
                              consul_rkey: str, consul_object: AnyDict) -> HandlerResult:
        # resource_identifier = f'consul-{consul_rkey}'

        endpoints = consul_object.get('Endpoints', [])
        name = consul_object.get('Service', consul_rkey)

        if len(endpoints) < 1:
            # Bzzt.
            self.logger.debug(f"ignoring Consul service {name} with no Endpoints")
            return None

        # We can turn this directly into an Ambassador Service resource, since Consul keeps
        # services and endpoints together (as it should!!).
        #
        # Note that we currently trust the association ID to contain the datacenter name.
        # That's a function of the watch_hook putting it there.

        svc = {
            'apiVersion': 'ambassador/v1',
            'ambassador_id': Config.ambassador_id,
            'kind': 'Service',
            'name': name,
            'datacenter': consul_object.get('Id') or 'dc1',
            'endpoints': {}
        }

        for ep in endpoints:
            ep_addr = ep.get('Address')
            ep_port = ep.get('Port')

            if not ep_addr or not ep_port:
                self.logger.debug(f"ignoring Consul service {name} endpoint {ep['ID']} missing address info")
                continue

            # Consul services don't have the weird indirections that Kube services do, so just
            # lump all the endpoints together under the same source port of '*'.
            svc_eps = svc['endpoints'].setdefault('*', [])
            svc_eps.append({
                'ip': ep_addr,
                'port': ep_port,
                'target_kind': 'Consul'
            })

        # Once again: don't return this. Instead, save it in self.services.
        self.services[f"consul-{name}-{svc['datacenter']}"] = svc

        return None

    def finalize(self) -> None:
        # The point here is to sort out self.k8s_services and self.k8s_endpoints and
        # turn them into proper Ambassador Service resources. This is a bit annoying,
        # because of the annoyances of Kubernetes, but we'll give it a go.
        #
        # Here are the rules:
        #
        # 1. By the time we get here, we have a _complete_ set of Ambassador resources that
        #    have passed muster by virtue of having the correct namespace, the correct
        #    ambassador_id, etc. (They may have duplicate names at this point, admittedly.)
        #    Any service not mentioned by name is out. Since the Ambassador resources in
        #    self.elements are in fact AResources, we can farm this out to code for each
        #    resource.
        #
        # 2. The check is, by design, permissive. If in doubt, write the check to leave
        #    the resource in.
        #
        # 3. For any service that stays in, we vet its listed ports against self.k8s_endpoints.
        #    Anything with no matching ports is _not_ dropped; it is assumed to use service
        #    routing rather than endpoint routing.

        od = {
            'elements': [ x.as_dict() for x in self.elements ],
            'k8s_endpoints': self.k8s_endpoints,
            'k8s_services': self.k8s_services,
            'services': self.services
        }

        # self.logger.debug("==== FINALIZE START\n%s" % json.dumps(od, sort_keys=True, indent=4))

        for key, k8s_svc in self.k8s_services.items():
            # See if we can find endpoints for this service.
            k8s_ep = self.k8s_endpoints.get(key, None)
            k8s_ep_ports = k8s_ep.get('ports', None) if k8s_ep else None

            k8s_name = k8s_svc['name']
            k8s_namespace = k8s_svc['namespace']

            # OK, Kube is weird. The way all this works goes like this:
            #
            # 1. When you create a Kube Service, Kube will allocate a clusterIP
            #    for it and update DNS to resolve the name of the service to
            #    that clusterIP.
            # 2. Kube will look over the pods matched by the Service's selectors
            #    and stick those pods' IP addresses into Endpoints for the Service.
            # 3. The Service will have ports listed. These service.port entries can
            #    contain:
            #      port -- a port number you can talk to at the clusterIP
            #      name -- a name for this port
            #      targetPort -- a port number you can talk to at the _endpoint_ IP
            #    We'll call the 'port' entry here the "service-port".
            # 4. If you talk to clusterIP:service-port, you will get magically
            #    proxied by the Kube CNI to a target port at one of the endpoint IPs.
            #
            # The $64K question is: how does Kube decide which target port to use?
            #
            # First, if there's only one endpoint port, that's the one that gets used.
            #
            # If there's more than one, if the Service's port entry has a targetPort
            # number, it uses that. Otherwise it tries to find an endpoint port with
            # the same name as the service port. Otherwise, I dunno, it punts and uses
            # the service-port.
            #
            # So that's how Ambassador is going to do it, for each Service port entry.
            #
            # If we have no endpoints at all, Ambassador will end up routing using
            # just the service name and port per the Mapping's service spec.

            target_ports = {}
            target_addrs = []
            svc_endpoints = {}

            if not k8s_ep or not k8s_ep_ports:
                # No endpoints at all, so we're done with this service.
                self.logger.debug(f'{key}: no endpoints at all')
            else:
                idx = -1

                for port in k8s_svc['ports']:
                    idx += 1

                    k8s_target: Optional[int] = None

                    src_port = port.get('port', None)

                    if not src_port:
                        # WTFO. This is impossible.
                        self.logger.error(f"Kubernetes service {key} has no port number at index {idx}?")
                        continue

                    if len(k8s_ep_ports) == 1:
                        # Just one endpoint port. Done.
                        k8s_target = list(k8s_ep_ports.values())[0]
                        target_ports[src_port] = k8s_target

                        self.logger.debug(f'{key} port {src_port}: single endpoint port {k8s_target}')
                        continue

                    # Hmmm, we need to try to actually map whatever ports are listed for
                    # this service. Oh well.

                    found_key = False
                    fallback: Optional[int] = None

                    for attr in [ 'targetPort', 'name', 'port' ]:
                        port_key = port.get(attr)   # This could be a name or a number, in general.

                        if port_key:
                            found_key = True

                            if not fallback and (port_key != 'name') and str(port_key).isdigit():
                                # fallback can only be digits.
                                fallback = port_key

                            # Do we have a destination port for this?
                            k8s_target = k8s_ep_ports.get(str(port_key), None)

                            if k8s_target:
                                self.logger.debug(f'{key} port {src_port} #{idx}: {attr} {port_key} -> {k8s_target}')
                                break
                            else:
                                self.logger.debug(f'{key} port {src_port} #{idx}: {attr} {port_key} -> miss')

                    if not found_key:
                        # WTFO. This is impossible.
                        self.logger.error(f"Kubernetes service {key} port {src_port} has an empty port spec at index {idx}?")
                        continue

                    if not k8s_target:
                        # This is most likely because we don't have endpoint info at all, so we'll do service
                        # routing.
                        #
                        # It's actually impossible for fallback to be unset, but WTF.
                        k8s_target = fallback or src_port

                        self.logger.debug(f'{key} port {src_port} #{idx}: falling back to {k8s_target}')

                    target_ports[src_port] = k8s_target

                if not target_ports:
                    # WTFO. This is impossible. I guess we'll fall back to service routing.
                    self.logger.error(f"Kubernetes service {key} has no routable ports at all?")

                # OK. Once _that's_ done we have to take the endpoint addresses into
                # account, or just use the service name if we don't have that.

                k8s_ep_addrs = k8s_ep.get('addresses', None)

                if k8s_ep_addrs:
                    for addr in k8s_ep_addrs:
                        ip = addr.get('ip', None)

                        if ip:
                            target_addrs.append(ip)

            # OK! If we have no target addresses, just use service routing.

            if not target_addrs:
                self.logger.debug(f'{key} falling back to service routing')
                target_addrs = [ key ]

            for src_port, target_port in target_ports.items():
                svc_endpoints[src_port] = [ {
                    'ip': target_addr,
                    'port': target_port
                } for target_addr in target_addrs ]

            # Nope. Set this up for service routing.
            self.services[f'k8s-{k8s_name}-{k8s_namespace}'] = {
                'apiVersion': 'ambassador/v1',
                'ambassador_id': Config.ambassador_id,
                'kind': 'Service',
                'name': k8s_name,
                'namespace': k8s_namespace,
                'endpoints': svc_endpoints
            }

        # OK. After all that, go turn all of the things in self.services into Ambassador
        # Service resources.

        for key, svc in self.services.items():
            serialization = dump_yaml(svc, default_flow_style=False)

            r = ACResource.from_dict(key, key, serialization, svc)
            self.elements.append(r)

        od = {
            'elements': [ x.as_dict() for x in self.elements ],
            'k8s_endpoints': self.k8s_endpoints,
            'k8s_services': self.k8s_services,
            'services': self.services
        }

        # self.logger.debug("==== FINALIZE END\n%s" % json.dumps(od, sort_keys=True, indent=4))
