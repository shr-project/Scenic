
import random
import time

from scenic.core.distributions import Samplable, RejectionException, needsSampling
from scenic.core.lazy_eval import needsLazyEvaluation
from scenic.core.external_params import ExternalSampler
from scenic.core.workspaces import Workspace
from scenic.core.vectors import Vector
from scenic.core.utils import areEquivalent, InvalidScenarioError

class Scene:
	"""A scene generated from a Scenic scenario"""
	def __init__(self, workspace, objects, egoObject, params):
		self.workspace = workspace
		self.objects = tuple(objects)
		self.egoObject = egoObject
		self.params = params

	def show(self, zoom=None, block=True):
		"""Render a schematic of the scene for debugging"""
		import matplotlib.pyplot as plt
		# display map
		self.workspace.show(plt)
		# draw objects
		for obj in self.objects:
			obj.show(self.workspace, plt, highlight=(obj is self.egoObject))
		# zoom in if requested
		if zoom != None:
			self.workspace.zoomAround(plt, self.objects, expansion=zoom)
		plt.show(block=block)

class Scenario:
	"""A Scenic scenario"""
	def __init__(self, workspace,
	             objects, egoObject,
	             params, externalParams,
	             requirements, requirementDeps):
		if workspace is None:
			workspace = Workspace()		# default empty workspace
		self.workspace = workspace
		ordered = []
		for obj in objects:
			ordered.append(obj)
			if obj is egoObject:	# make ego the first object
				ordered[0], ordered[-1] = ordered[-1], ordered[0]
		assert ordered[0] is egoObject
		self.objects = tuple(ordered)
		self.egoObject = egoObject
		self.params = dict(params)
		self.externalParams = tuple(externalParams)
		self.requirements = tuple(requirements)
		self.externalSampler = ExternalSampler.forParameters(self.externalParams, self.params)
		# dependencies must use fixed order for reproducibility
		paramDeps = tuple(p for p in self.params.values() if isinstance(p, Samplable))
		self.dependencies = self.objects + paramDeps + tuple(requirementDeps)
		self.validate()

	def isEquivalentTo(self, other):
		if type(other) is not Scenario:
			return False
		return (areEquivalent(other.workspace, self.workspace)
		    and areEquivalent(other.objects, self.objects)
		    and areEquivalent(other.params, self.params)
		    and areEquivalent(other.externalParams, self.externalParams)
		    and areEquivalent(other.requirements, self.requirements)
		    and other.externalSampler == self.externalSampler)

	def containerOfObject(self, obj):
		if hasattr(obj, 'regionContainedIn') and obj.regionContainedIn is not None:
			return obj.regionContainedIn
		else:
			return self.workspace.region

	def validate(self):
		"""Make some simple static checks for inconsistent built-in requirements."""
		objects = self.objects
		staticVisibility = not needsSampling(self.egoObject.visibleRegion)
		staticBounds = [self.hasStaticBounds(obj) for obj in objects]
		for i in range(len(objects)):
			oi = objects[i]
			# skip objects with unknown positions or bounding boxes
			if not staticBounds[i]:
				continue
			# Require object to be contained in the workspace/valid region
			container = self.containerOfObject(oi)
			if not needsSampling(container) and not container.containsObject(oi):
				raise InvalidScenarioError(f'Object at {oi.position} does not fit in container')
			# Require object to be visible from the ego object
			if staticVisibility and oi.requireVisible is True and oi is not self.egoObject:
				if not self.egoObject.canSee(oi):
					raise InvalidScenarioError(f'Object at {oi.position} is not visible from ego')
			# Require object to not intersect another object
			for j in range(i):
				oj = objects[j]
				if not staticBounds[j]:
					continue
				if oi.intersects(oj):
					raise InvalidScenarioError(f'Object at {oi.position} intersects'
					                           f' object at {oj.position}')

	def hasStaticBounds(self, obj):
		if needsSampling(obj.position):
			return False
		if any(needsSampling(corner) for corner in obj.corners):
			return False
		return True

	def generate(self, maxIterations=2000, verbosity=0, feedback=None):
		objects = self.objects

		# choose which custom requirements will be enforced for this sample
		activeReqs = [req for req, prob in self.requirements if random.random() <= prob]

		# do rejection sampling until requirements are satisfied
		rejection = True
		iterations = 0
		while rejection is not None:
			if iterations > 0:	# rejected the last sample
				if verbosity >= 2:
					print(f'  Rejected sample {iterations} because of: {rejection}')
				if self.externalSampler is not None:
					feedback = self.externalSampler.rejectionFeedback
			if iterations >= maxIterations:
				raise RuntimeError(f'failed to generate scenario in {iterations} iterations')
			iterations += 1
			try:
				if self.externalSampler is not None:
					self.externalSampler.sample(feedback)
				sample = Samplable.sampleAll(self.dependencies)
			except RejectionException as e:
				rejection = e
				continue
			rejection = None
			ego = sample[self.egoObject]
			# Normalize types of some built-in properties
			for obj in objects:
				sampledObj = sample[obj]
				assert not needsSampling(sampledObj)
				assert isinstance(sampledObj.position, Vector)
				sampledObj.heading = float(sampledObj.heading)
			# Check built-in requirements
			for i in range(len(objects)):
				vi = sample[objects[i]]
				# Require object to be contained in the workspace/valid region
				container = self.containerOfObject(vi)
				if not container.containsObject(vi):
					rejection = 'object containment'
					break
				# Require object to be visible from the ego object
				if vi.requireVisible and vi is not ego and not ego.canSee(vi):
					rejection = 'object visibility'
					break
				# Require object to not intersect another object
				for j in range(i):
					vj = sample[objects[j]]
					if vi.intersects(vj):
						rejection = 'object intersection'
						break
				if rejection is not None:
					break
			if rejection is not None:
				continue
			# Check user-specified requirements
			for req in activeReqs:
				if not req(sample):
					rejection = 'user-specified requirement'
					break

		# obtained a valid sample; assemble a scene from it
		sampledObjects = tuple(sample[obj] for obj in objects)
		sampledParams = {}
		for param, value in self.params.items():
			sampledValue = sample[value] if isinstance(value, Samplable) else value
			assert not needsLazyEvaluation(sampledValue)
			sampledParams[param] = sampledValue
		scene = Scene(self.workspace, sampledObjects, ego, sampledParams)
		return scene, iterations

	def resetExternalSampler(self):
		"""Reset the scenario's external sampler, if any.

		If the Python random seed is reset before calling this function, this
		should cause the sequence of generated scenes to be deterministic."""
		self.externalSampler = ExternalSampler.forParameters(self.externalParams, self.params)
